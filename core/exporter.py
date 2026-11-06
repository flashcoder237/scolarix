"""Exports tabulaires (Excel / CSV) pour le Décanat, et export du journal."""
from __future__ import annotations

import csv
from pathlib import Path

from .database import Releve

COLUMNS = [
    ("Matricule", lambda r: r.matricule),
    ("Nom", lambda r: r.nom or r.nom_complet),
    ("Prénom(s)", lambda r: r.prenom or ""),
    ("Date de naissance", lambda r: r.date_connue or ""),
    ("Filière", lambda r: r.filiere or ""),
    ("Niveau", lambda r: r.niveau or ""),
    ("Semestre", lambda r: r.semestre if r.semestre is not None else ""),
    ("Année académique", lambda r: r.annee_academique or ""),
    ("Session", lambda r: r.session or ""),
    ("Statut", lambda r: r.statut or ""),
    ("Décision du jury", lambda r: r.decision or ""),
    ("Moyenne", lambda r: to_number(r.moyenne)),
    ("Crédits", lambda r: r.credits if r.credits is not None else ""),
    ("Grade", lambda r: r.grade or ""),
    ("MGP", lambda r: to_number(r.mgp)),
    ("Mention", lambda r: mention(r.moyenne, r.grade)),
    ("Fichier source", lambda r: r.chemin),
    ("Pages", lambda r: f"{r.numero_page}-{r.page_fin}" if r.page_fin > r.numero_page else str(r.numero_page)),
]


OPTIONAL = {"Prénom(s)", "Session"}   # colonnes retirées de l'export quand elles sont vides partout
NUMERIC = {"Moyenne": "0.00", "MGP": "0.0", "Crédits": "0", "Semestre": "0"}

# Barème imprimé au verso des relevés FMSP (système LMD)
GRADE_MENTION = {"A+": "Excellent", "A": "Très bien", "A-": "Très bien", "B+": "Bien", "B": "Bien", "B-": "Bien",
                 "C+": "Assez bien", "C": "Passable", "C-": "Passable", "D+": "Médiocre", "D": "Médiocre",
                 "D-": "Médiocre", "E": "Éliminé", "F": "Éliminé"}


def to_number(value: str | None):
    """« 11,39 » -> 11.39 (un vrai nombre dans Excel : triable et calculable)."""
    try:
        return float((value or "").replace(",", ".")) if value else ""
    except ValueError:
        return value or ""


def mention(moyenne: str | None, grade: str | None = None) -> str:
    if grade and grade.replace(" ", "") in GRADE_MENTION:
        return GRADE_MENTION[grade.replace(" ", "")]
    m = to_number(moyenne)
    if m == "" or isinstance(m, str):
        return ""
    if m >= 18:
        return "Excellent"
    if m >= 16:
        return "Très bien"
    if m >= 14:
        return "Bien"
    if m >= 12:
        return "Assez bien"
    if m >= 10:
        return "Passable"
    return "Médiocre"


def columns_for(rels: list[Releve]) -> list[tuple]:
    return [(c, f) for c, f in COLUMNS if c not in OPTIONAL or any(f(r) for r in rels)]


def export_csv(rels: list[Releve], path: str | Path) -> Path:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM : Excel ouvre correctement les accents
        w = csv.writer(fh, delimiter=";")
        cols = columns_for(rels)
        w.writerow([c for c, _ in cols])
        for r in rels:
            w.writerow([str(v).replace(".", ",") if isinstance(v, float) else v for v in (f(r) for _, f in cols)])
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
    cols = columns_for(rels)
    ws.append([c for c, _ in cols])
    header_fill = PatternFill("solid", fgColor="1D4ED8")
    for cell in ws[3]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
    for r in rels:
        ws.append([f(r) for _, f in cols])
    widths = {"Matricule": 12, "Nom": 30, "Prénom(s)": 20, "Date de naissance": 14, "Filière": 22, "Niveau": 11,
              "Semestre": 9, "Année académique": 15, "Session": 12, "Statut": 13, "Décision du jury": 24,
              "Moyenne": 9, "Crédits": 8, "Grade": 7, "MGP": 7, "Mention": 12, "Fichier source": 60, "Pages": 8}
    for i, (c, _) in enumerate(cols, start=1):
        letter = get_column_letter(i)
        ws.column_dimensions[letter].width = widths.get(c, 12)
        if c in NUMERIC:
            for row in range(4, ws.max_row + 1):
                ws[f"{letter}{row}"].number_format = NUMERIC[c]
    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A3:{get_column_letter(len(cols))}{ws.max_row}"
    wb.save(str(path))
    return Path(path)


def export_journal_csv(rows, path: str | Path) -> Path:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Horodatage", "Opérateur", "Poste", "Action", "Matricule", "Détail"])
        for r in rows:
            w.writerow([r["horodatage"], r["operateur"], r["poste"], r["action"], r["matricule"] or "", r["detail"] or ""])
    return Path(path)
