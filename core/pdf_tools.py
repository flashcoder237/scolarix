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


TEMP_DIR = Path(tempfile.gettempdir()) / "scolarix_tmp"


def temp_pdf(prefix: str = "releve_") -> Path:
    """Fichier temporaire dans un dossier dédié, vidé au démarrage et à la fermeture (données personnelles)."""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    fd = tempfile.NamedTemporaryFile(prefix=prefix, suffix=".pdf", delete=False, dir=TEMP_DIR)
    fd.close()
    return Path(fd.name)


def purge_temp() -> int:
    n = 0
    if TEMP_DIR.exists():
        for f in TEMP_DIR.glob("*.pdf"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


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
    c.setFillColor(Color(0.72, 0.11, 0.11, alpha=0.75))   # sous-titre horodaté lisible sur le filigrane
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
        dest = temp_pdf()
    with open(dest, "wb") as fh:
        writer.write(fh)
    return Path(dest)


def _cover_page(rels: list[Releve], title: str, page_labels: list[str] | None = None,
                absent: list[str] | None = None) -> PdfReader:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, h - 60, "UNIVERSITÉ DE DOUALA — FACULTÉ DE MÉDECINE ET DES SCIENCES PHARMACEUTIQUES")
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, h - 100, title)
    # Nom de l'étudiant : celui du relevé le plus récent (les anciens fichiers ont parfois des noms collés)
    latest = max(rels, key=lambda r: (r.annee_academique or "", r.niveau_num, r.semestre or 0, r.id))
    c.setFont("Helvetica", 11)
    c.drawString(50, h - 128, f"{latest.nom_complet}   ·   matricule(s) : {', '.join(sorted({r.matricule for r in rels}))}")
    c.drawString(50, h - 146, f"Édité le {datetime.now():%d/%m/%Y à %H:%M} par la Cellule Informatique")
    y = h - 172
    if absent:
        c.setFillColor(Color(0.72, 0.11, 0.11))
        c.setFont("Helvetica-Bold", 10)
        c.drawString(50, y, f"Dossier incomplet — période(s) non trouvée(s) dans les archives : {', '.join(absent)}"[:110])
        c.setFillColor(Color(0, 0, 0))
        y -= 18
    y -= 14
    c.setFont("Helvetica-Bold", 10)
    for x, label in ((50, "#"), (72, "Niveau · semestre"), (200, "Année"), (262, "Moyenne"), (318, "Crédits"),
                     (364, "Décision du jury"), (505, "Pages")):
        c.drawString(x, y, label)
    c.setFont("Helvetica", 9.5)
    for i, r in enumerate(rels, start=1):
        y -= 17
        if y < 60:
            c.showPage()
            y = h - 60
            c.setFont("Helvetica", 9.5)
        c.drawString(50, y, str(i))
        c.drawString(72, y, r.periode[:24])
        c.drawString(200, y, r.annee_academique or "?")
        c.drawString(262, y, r.moyenne or "—")
        c.drawString(318, y, str(r.credits) if r.credits is not None else "—")
        c.drawString(364, y, (r.decision or r.statut or "?")[:26])
        label = page_labels[i - 1] if page_labels else ""
        c.drawString(505, y, label)
    c.showPage()
    c.save()
    buf.seek(0)
    return PdfReader(buf)


def merge_releves(rels: list[Releve], dest: str | Path, cover: bool = True,
                  stamp: tuple[str, str] | None = None, title: str = "Cursus académique",
                  prepare=None, absent: list[str] | None = None) -> tuple[Path, int, list[str]]:
    """Fusionne plusieurs relevés (dans l'ordre donné) en un seul PDF. Renvoie (chemin, nb pages, manquants).

    ``prepare(rel) -> rel`` produit au besoin l'aperçu PDF d'un relevé Word (voir core/preview.py).
    ``absent`` : périodes attendues mais introuvables, signalées sur la page de garde."""
    missing: list[str] = []
    chunks: list[tuple[Releve, PdfReader, range]] = []
    readers: dict[str, PdfReader] = {}
    for r in rels:
        if prepare is not None:
            try:
                r = prepare(r)
            except Exception:
                missing.append(f"{r.periode} {r.annee_academique or ''}".strip())
                continue
        src = r.preview_pdf
        if not src or not Path(src).exists():
            missing.append(f"{r.periode} {r.annee_academique or ''}".strip())
            continue
        reader = readers.setdefault(src, PdfReader(src))
        chunks.append((r, reader, range(r.numero_page - 1, min(r.page_fin, len(reader.pages)))))
    writer = PdfWriter()
    kept = [c[0] for c in chunks]
    if cover and kept:
        n_cover = len(_cover_page(kept, title, None, absent).pages)
        labels, pos = [], n_cover + 1
        for _, _, pages in chunks:
            labels.append(f"p. {pos}" if len(pages) == 1 else f"p. {pos}–{pos + len(pages) - 1}")
            pos += len(pages)
        for page in _cover_page(kept, title, labels, (absent or []) + missing).pages:
            writer.add_page(page)
    for _, reader, pages in chunks:
        for i in pages:
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
