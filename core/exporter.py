"""Exports tabulaires (Excel / CSV) pour le Décanat, et export du journal."""
from __future__ import annotations

import csv
from pathlib import Path

from .database import Releve

COLUMNS = [
    ("Matricule", lambda r: r.matricule),
    ("Nom", lambda r: r.nom or r.nom_complet),
    ("Prénom(s)", lambda r: r.prenom or ""),
    ("Date de naissance", lambda r: r.date_naissance or ""),
    ("Filière", lambda r: r.filiere or ""),
    ("Niveau", lambda r: r.niveau or ""),
    ("Semestre", lambda r: r.semestre if r.semestre is not None else ""),
    ("Année académique", lambda r: r.annee_academique or ""),
    ("Session", lambda r: r.session or ""),
    ("Statut", lambda r: r.statut or ""),
    ("Décision du jury", lambda r: r.decision or ""),
    ("Moyenne", lambda r: r.moyenne or ""),
    ("Crédits", lambda r: r.credits if r.credits is not None else ""),
    ("Grade", lambda r: r.grade or ""),
    ("MGP", lambda r: r.mgp or ""),
    ("Mention", lambda r: mention(r.moyenne)),
    ("Fichier source", lambda r: r.chemin),
    ("Pages", lambda r: f"{r.numero_page}-{r.page_fin}" if r.page_fin > r.numero_page else str(r.numero_page)),
]


def mention(moyenne: str | None) -> str:
    try:
        m = float((moyenne or "").replace(",", "."))
    except ValueError:
        return ""
    if m >= 16:
        return "Très bien"
    if m >= 14:
        return "Bien"
    if m >= 12:
        return "Assez bien"
    if m >= 10:
        return "Passable"
    return ""


def export_csv(rels: list[Releve], path: str | Path) -> Path:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM : Excel ouvre correctement les accents
        w = csv.writer(fh, delimiter=";")
        w.writerow([c for c, _ in COLUMNS])
        for r in rels:
            w.writerow([f(r) for _, f in COLUMNS])
    return Path(path)


def export_xlsx(rels: list[Releve], path: str | Path, title: str = "Extraction FMSP") -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Extraction"
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append([c for c, _ in COLUMNS])
    header_fill = PatternFill("solid", fgColor="1D4ED8")
    for cell in ws[3]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
    for r in rels:
        ws.append([f(r) for _, f in COLUMNS])
    widths = [12, 22, 22, 14, 24, 12, 9, 16, 12, 12, 22, 10, 9, 8, 8, 12, 60, 8]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A3:{get_column_letter(len(COLUMNS))}{ws.max_row}"
    wb.save(str(path))
    return Path(path)


def export_journal_csv(rows, path: str | Path) -> Path:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Horodatage", "Opérateur", "Poste", "Action", "Matricule", "Détail"])
        for r in rows:
            w.writerow([r["horodatage"], r["operateur"], r["poste"], r["action"], r["matricule"] or "", r["detail"] or ""])
    return Path(path)
