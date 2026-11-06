"""Formats de relevés (annuels, crédits manquants…), recherche, extraction, exports, PDF."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest
from conftest import make_settings
from docx import Document
from pypdf import PdfReader

from core import exporter, pdf_tools
from core.database import Database, Releve, missing_periods, resolve_cache
from core.extractors import PageText, docx_pages_text
from core.parsing import detect_statut, extract_recap, parse_pages

# ------------------------------------------------------- formats (données anonymisées)
ANNUEL = """RELEVE DE NOTES / TRANSCRIPT
NOM ET PRENOM: ETUDIANTE TEST ANNUELLE MATRICULE: 98MP106
surname and name: Registration N°:
NÉ(E) LE: 04/01/2000 A: VILLE
CYCLE: DOCTORAT ANNÉE ACADÉMIQUE: 2024-2025 FILIÈRE: PHARMACIE
NIVEAU: 7 OPTION: BIOLOGIE
UE P713 – UE P714 REDACTION – SOUTENANCE THESE 19.00 18.40 60
RELEVE NIVEAU TOTAL CREDIT ANNUEL MOYENNE ANNUELLE / 20 MGP GRADE DECISION DU JURY
7 60 18.40 4.0 A+ SEMESTRES VALIDES
"""

RELATIF = """RELEVE DE NOTES / TRANSCRIPT
NOM ET PRENOM: ETUDIANT TEST RELATIF MATRICULE: 98MP105
NÉ(E) LE: 21/02/1992 A: VILLE
CYCLE: DOCTORAT ANNÉE ACADÉMIQUE: 2022-2023 FILIÈRE: PHARMACIE
NIVEAU: VI SEMESTRE: I OPTION: BIOLOGIE
RELEVE NIVEAU SEMESTRE TOTAL CREDIT / 30 MOYENNE SEMESTRIELLE / 20 MGP GRADE DECISION DU JURY
6 11 30 11,54 2,3 C+ SEMESTRE VALIDE
"""

SANS_CREDITS = """RELEVE DE NOTES / TRANSCRIPT
NOM ET PRENOM: ETUDIANT SANS CREDIT MATRICULE: 98MP107
NÉ(E) LE: 00/01/1900 A: VILLE
CYCLE: LICENCE ANNÉE ACADÉMIQUE: 2018-2019 FILIÈRE: PHARMACIE
NIVEAU: I SEMESTRE: I
UE P111 CHIMIE GENERALE 2,00 2,00 0 NON VALIDEE
RELEVE NIVEAU SEMESTRE TOTAL CREDIT / 30 MOYENNE SEMESTRIELLE / 20 GRADE DECISION DU JURY
1 1 1,83 E SEMESTRE NON VALIDE
"""


def test_releve_annuel_doctorat():
    r = parse_pages([PageText(1, ANNUEL)], "x.pdf")[0][0]
    assert (r["matricule"], r["niveau"], r["semestre"], r["semestre_fin"]) == ("98MP106", "Niveau 7", 13, 14)
    assert (r["credits"], r["moyenne"], r["mgp"], r["grade"]) == (60, "18,40", "4,0", "A+")
    assert (r["statut"], r["decision"]) == ("VALIDÉ", "SEMESTRES VALIDES")
    assert (r["cycle"], r["option_filiere"], r["filiere_code"]) == ("DOCTORAT", "BIOLOGIE", "P")


def test_semestre_relatif_converti_en_absolu():
    r = parse_pages([PageText(1, RELATIF)], "x.pdf")[0][0]
    assert (r["niveau"], r["semestre"], r["semestre_fin"]) == ("Niveau 6", 11, 11)
    assert r["statut"] == "VALIDÉ" and r["credits"] == 30


def test_case_credits_vide_et_date_fictive():
    r = parse_pages([PageText(1, SANS_CREDITS)], "x.pdf")[0][0]
    assert (r["moyenne"], r["grade"], r["credits"]) == ("1,83", "E", None)
    assert (r["statut"], r["decision"]) == ("NON VALIDÉ", "SEMESTRE NON VALIDE")   # jamais « 1 UE non validée »
    assert r["date_naissance"] is None                                             # 00/01/1900 = gabarit


@pytest.mark.parametrize("line,expected", [
    ("1 2 24 9,48 1,7 C- SEMESTRE NON VALIDE", (1, 2, 24, "9,48", "C-", False)),
    ("2 3 30 12,31 2,7 B- SEMESTRE VALIDE", (2, 3, 30, "12,31", "B-", False)),
    ("7 60 18.40 A+ 4.0 SEMESTRES VALIDES", (7, None, 60, "18,40", "A+", True)),
    ("1 1 1,83 E SEMESTRE NON VALIDE", (1, 1, None, "1,83", "E", False)),
])
def test_ligne_recapitulative(line, expected):
    r = extract_recap(line)
    assert (r["niveau"], r["semestre"], r["credits"], r["moyenne"], r["grade"], r["annuel"]) == expected


def test_colonne_jury_ancien_format_cellule_suivante_collee():
    text = "17 SOINS INFIRMIERS NURSING 12,20 12,20 B- 4 VALIDEE\n18 SCIENCES SOCIALES 13,05 B- 6 VALIDEE FORMATION"
    assert detect_statut(text) == ("VALIDÉ", "2 UE validées sur 2")


def test_docx_releve_deborde_sur_page_suivante_sans_saut(tmp_path):
    """Deux relevés dans un .docx sans saut de page enregistré : coupés sur l'en-tête du second."""
    doc = Document()
    for nom, mat in (("PREMIER", "98EM101"), ("SECOND", "98EM103")):
        doc.add_paragraph(f"NOM ET PRENOM: {nom} ETUDIANT")
        doc.add_paragraph(f"MATRICULE : {mat}Registration N°:")   # libellé anglais collé (zone de texte)
        doc.add_paragraph("NIVEAU: III SEMESTRE: V ANNÉE ACADÉMIQUE: 2013 - 2014")
        doc.add_paragraph("19 IMMUNOLOGIE 14,50 B+ 4 VALIDEE")
    doc.save(str(tmp_path / "x.docx"))
    pages = docx_pages_text(tmp_path / "x.docx")
    assert len(pages) == 2
    rels, _ = parse_pages(pages, "x.docx")
    assert [r["matricule"] for r in rels] == ["98EM101", "98EM103"]


# ------------------------------------------------------------------- base & recherche
def test_recherche_saisies_mixtes_et_variantes(indexed):
    db = indexed[0]
    assert {r.matricule for r in db.search("18M42")} == {"18M042"}      # zéro omis
    assert {r.matricule for r in db.search("18M042 tchoua")} == {"18M042"}
    assert all(r.matricule == "18M042" and "2019" in (r.annee_academique or "") for r in db.search("18M042 2019"))
    assert {r.matricule for r in db.search("M042")} >= {"18M042", "19M042"}
    assert db.search("a") == [] or all(" A " in f" {r.nom_norm} " for r in db.search("a"))   # pas de bruit
    assert db.search("' OR 1=1 --") == []


def test_resultats_groupes_et_chronologiques(indexed):
    db = indexed[0]
    res = db.search("tchoua")
    mats = [r.matricule for r in res]
    # chaque étudiant forme un bloc contigu
    assert all(mats.index(m) + mats.count(m) - 1 == len(mats) - 1 - mats[::-1].index(m) for m in set(mats))
    alain = [r for r in res if r.matricule == "18M042"]
    keys = [(r.niveau_num, r.semestre or 0, r.annee_academique or "") for r in alain]
    assert keys == sorted(keys)
    exact_first = res[0]
    assert exact_first.score == 100


def test_extraction_dernier_niveau_puis_statut(indexed, tmp_path):
    db = indexed[0]
    tous = db.extraction("M", None, None, "tous", dernier_niveau=True)
    non_valides = db.extraction("M", None, None, "non_valides", dernier_niveau=True)
    admis = db.extraction("M", None, None, "admis", dernier_niveau=True)
    assert len(non_valides) + len(admis) == len(tous)          # partition des étudiants par DERNIER relevé
    assert all(not r.is_success for r in non_valides) and all(r.is_success for r in admis)


def test_periodes_manquantes_et_releve_annuel():
    def rel(sem, fin=None):
        return Releve(id=1, fichier_id=1, matricule="X", matricule_brut=None, nom=None, prenom=None, nom_complet="X",
                      nom_norm="X", date_naissance=None, filiere=None, filiere_code=None, niveau="Niveau 1",
                      niveau_num=1, annee_academique=None, session=None, statut=None, moyenne=None, numero_page=1,
                      page_fin=1, ocr_confiance=None, extrait=None, semestre=sem, decision=None, credits=None,
                      grade=None, mgp=None, lieu_naissance=None, semestre_fin=fin or sem, cycle=None,
                      option_filiere=None, chemin="x", type_fichier="PDF_TEXT", cache_pdf=None, fichier_statut="OK")
    assert missing_periods([rel(1), rel(2), rel(5)]) == ["S3", "S4"]
    assert missing_periods([rel(1), rel(2), rel(3, 4)]) == []           # annuel : couvre S3 et S4
    r = rel(13, 14)
    assert r.annuel and r.periode == "Niveau 1 · S13-S14 (annuel)"
    r.decision, r.date_naissance = "5 UE validées sur 5", "00/01/1900"
    assert r.decision_deduite and r.date_connue is None


def test_cache_relatif_portable(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "abc.pdf").write_bytes(b"%PDF")
    assert resolve_cache("abc.pdf", cache) == str(cache / "abc.pdf")
    assert resolve_cache("/ancien/poste/cache/abc.pdf", cache) == str(cache / "abc.pdf")   # base copiée
    assert resolve_cache(None, cache) is None


def test_enregistrement_atomique_avec_anomalies(tmp_path, data_dir):
    s = make_settings(tmp_path)
    db = Database(s.db_path)
    db.store_result("/a/x.pdf", "PDF_TEXT", 10, 1.0, 1, None, "OK", None,
                    [{"matricule": "18M042", "nom_complet": "TEST", "numero_page": 1, "page_fin": 1}],
                    [("AVERTISSEMENT", "un avertissement", None)])
    assert db.count_releves() == 1 and len(db.anomalies()) == 1
    db.store_result("/a/x.pdf", "PDF_TEXT", 10, 2.0, 1, None, "OK", None, [], [])   # réindexé : remplacé
    assert db.count_releves() == 0 and db.anomalies() == []
    assert db.file_signatures()["/a/x.pdf"] == (10, 2.0)


# ------------------------------------------------------------------------ exports
def test_export_excel_nombres_et_mentions(indexed, tmp_path):
    from openpyxl import load_workbook
    db = indexed[0]
    rels = db.extraction(None, None, None)
    path = exporter.export_xlsx(rels, tmp_path / "x.xlsx")
    ws = load_workbook(path).active
    header = [c.value for c in ws[3]]
    assert "Prénom(s)" in header or all(not r.prenom for r in rels)
    col = header.index("Moyenne") + 1
    values = [ws.cell(row=i, column=col).value for i in range(4, ws.max_row + 1)]
    assert any(isinstance(v, float) for v in values)                    # vrais nombres, triables
    assert exporter.mention("18,5") == "Excellent" and exporter.mention("9,5") == "Médiocre"
    assert exporter.mention("11,0", "C+") == "Assez bien"               # le grade prime


def test_export_csv(indexed, tmp_path):
    db = indexed[0]
    rels = db.extraction(None, None, None)[:5]
    path = exporter.export_csv(rels, tmp_path / "x.csv")
    rows = list(csv.reader(open(path, encoding="utf-8-sig"), delimiter=";"))
    assert rows[0][0] == "Matricule" and len(rows) == 6
    assert "Session" not in rows[0] or any(r.session for r in rels)


# --------------------------------------------------------------------------- PDF
def test_fusion_page_de_garde_periodes_absentes(indexed, tmp_path):
    db, settings = indexed[0], indexed[1]
    from core.preview import PreviewService
    svc = PreviewService(db, settings)
    cur = pdf_tools.select_for_cursus(db.cursus("18M042"))
    path, n, missing = pdf_tools.merge_releves(cur, tmp_path / "c.pdf", prepare=svc.ensure,
                                               absent=missing_periods(db.cursus("18M042")))
    cover = PdfReader(str(path)).pages[0].extract_text()
    # Le fonds de démo a des relevés ANNUELS (N1…N5, N7) : le niveau 6 = semestres S11-S12 manquants
    assert "Dossier incomplet" in cover and "S11, S12" in cover
    assert "p. 2" in cover                                               # pages du document fusionné
    svc.close()


def test_timbre_et_fichiers_temporaires(indexed):
    db = indexed[0]
    rel = next(r for r in db.search("18M042") if r.type_fichier.startswith("PDF"))
    tmp = pdf_tools.extract_pages(rel.preview_pdf, rel.numero_page, rel.page_fin, stamp=pdf_tools.stamp_text())
    assert Path(tmp).parent == pdf_tools.TEMP_DIR
    assert "DUPLICATA" in PdfReader(str(tmp)).pages[0].extract_text()
    assert pdf_tools.purge_temp() >= 1 and not Path(tmp).exists()
    assert pdf_tools.export_filename(rel).startswith("FMSP_18M042_")
