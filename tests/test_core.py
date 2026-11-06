"""Tests du cœur métier sur un faux fonds d'archives généré à la volée.

    pytest -q
"""
from __future__ import annotations

import pytest
from conftest import HAS_CONVERTER, HAS_TESSERACT

from core import pdf_tools  # noqa: E402
from core.extractors import PageText  # noqa: E402
from core.indexer import Indexer, check_integrity  # noqa: E402
from core.normalize import find_matricules, norm_matricule, norm_text  # noqa: E402
from core.parsing import hints_from_path, parse_pages, split_nom_prenom  # noqa: E402


# --------------------------------------------------------------- normalisation
@pytest.mark.parametrize("raw", ["18M042", "18-M-042", "18/M/042", "18 m 042", "18.M.042"])
def test_norm_matricule(raw):
    assert norm_matricule(raw) == "18M042"


def test_find_matricules_ignores_times_and_dates():
    assert find_matricules("Fait le 12 de 2021 à 20 h 30, matricule 17/M/090") == ["17M090"]


def test_norm_text_apostrophes_accents():
    assert norm_text("N'DOUKOU Arlétte") == "NDOUKOU ARLETTE"


def test_split_nom_prenom():
    assert split_nom_prenom("TCHOUA NGUEMO Brice") == ("TCHOUA NGUEMO", "Brice")


# ------------------------------------------------------------------- parsing
def _page(n, text):
    return PageText(n, text)


def test_two_pages_linked_without_header():
    p1 = "RELEVÉ DE NOTES\nAnnée académique : 2021-2022\nNoms et Prénoms : TCHOUA Alain\nMatricule : 18M042\n" \
         "Filière : Médecine Générale  Niveau : 4\nMED401 Sémiologie 6 13,70 Validée\n... suite au verso"
    p2 = "MED405 Anatomie 5 12,80 Validée\nMoyenne annuelle : 13,15 / 20\nDécision du jury : ADMIS"
    p3 = "RELEVÉ DE NOTES\nAnnée académique : 2021-2022\nNoms et Prénoms : KAMGA Josiane\nMatricule : 19-M-042\n" \
         "Niveau : 4\nDécision du jury : AJOURNÉ"
    rels, _ = parse_pages([_page(34, p1), _page(35, p2), _page(36, p3)], r"X:\2021-2022\Medecine\N4_SN.pdf")
    assert [(r["matricule"], r["numero_page"], r["page_fin"]) for r in rels] == [("18M042", 34, 35), ("19M042", 36, 36)]
    assert rels[0]["statut"] == "ADMIS" and rels[1]["statut"] == "AJOURNÉ"
    assert rels[0]["niveau"] == "Niveau 4" and rels[0]["filiere_code"] == "M"


def test_ue_validee_is_not_a_decision():
    text = "Matricule : 18M042\nNoms et Prénoms : TCHOUA Alain\nMED301 Immunologie 4 8,50 Non validée\n" \
           "Décision du jury : AJOURNÉ"
    rels, _ = parse_pages([_page(1, text)], "x.pdf")
    assert rels[0]["statut"] == "AJOURNÉ"


def test_hints_from_path():
    h = hints_from_path(r"\\SRV\Archives\2020-2021\Medecine\N3_SR_Releves.docx")
    assert (h.annee, h.filiere_code, h.session, h.niveau) == ("2020-2021", "M", "Rattrapage", "Niveau 3")


# ---------------------------------------------------- indexation de bout en bout
# (fixture « indexed » : voir conftest.py)


def test_indexing_counts(indexed):
    db, _, _, counters = indexed
    assert counters.echecs == 1          # le .doc corrompu, sans arrêter le reste
    assert counters.ecrits >= 50
    assert any(a["niveau"] == "ERREUR" for a in db.anomalies())


def test_search_variants_and_fuzzy(indexed):
    db = indexed[0]
    assert {r.matricule for r in db.search("19-m/042")} == {"19M042"}
    names = {r.nom_complet: r.score for r in db.search("tchoua")}
    assert names["TCHOUWA Serge"] < 100      # résultat approché signalé
    assert "TCHOUWA Serge" not in {r.nom_complet for r in db.search("tchoua", tolerant=False)}
    assert db.search("ndoukou")[0].matricule == "17M090"


def test_cursus_and_rattrapage(indexed):
    db = indexed[0]
    cur = db.cursus("18M042")
    assert {r.niveau for r in cur} == {f"Niveau {n}" for n in (1, 2, 3, 4, 5, 7)}
    n3 = [r for r in cur if r.niveau == "Niveau 3"]
    assert sorted((r.session, r.statut) for r in n3) == [("Normale", "AJOURNÉ"), ("Rattrapage", "ADMIS")]
    sel = pdf_tools.select_for_cursus(cur)
    assert [(r.niveau, r.session) for r in sel if r.niveau == "Niveau 3"] == [("Niveau 3", "Rattrapage")]
    n4 = next(r for r in cur if r.niveau == "Niveau 4")
    assert n4.nb_pages == 2 and "2 PAGES LIÉES" in n4.flags
    assert db.correlated_matricules("18M042")[0]["matricule"] == "15B077"




@pytest.mark.skipif(not HAS_TESSERACT, reason="Tesseract non installé")
def test_ocr_scan(indexed):
    db = indexed[0]
    r = db.search("marie claire")[0]
    assert r.matricule == "19P017" and r.type_fichier == "PDF_SCAN"


def test_pdf_tools(indexed, tmp_path):
    db, settings = indexed[0], indexed[1]
    rel = next(r for r in db.cursus("18M042") if r.nb_pages == 2)
    out = pdf_tools.extract_pages(rel.preview_pdf, rel.numero_page, rel.page_fin, tmp_path / "x.pdf",
                                  stamp=pdf_tools.stamp_text())
    from pypdf import PdfReader
    assert len(PdfReader(str(out)).pages) == 2
    from core.preview import PreviewService
    svc = PreviewService(db, settings)
    path, n, missing = pdf_tools.merge_releves(pdf_tools.select_for_cursus(db.cursus("18M042")), tmp_path / "c.pdf",
                                               prepare=svc.ensure)
    if HAS_CONVERTER:
        assert n == 1 + 7 and not missing     # page de garde + 7 pages
    else:                                     # .docx sans aperçu : signalés comme manquants, jamais d'erreur
        assert n >= 1 and len(missing) <= 3


def test_read_only_sources(indexed):
    """Règle de non-destruction : aucun fichier source modifié ou créé dans les archives."""
    _, _, archives, _ = indexed
    import generate_samples  # noqa: F401
    names = sorted(p.name for p in archives.rglob("*") if p.is_file())
    assert all(not n.endswith(".pdf.tmp") for n in names)
    assert not any("cache" in str(p) for p in archives.rglob("*"))


def test_differential_and_integrity(indexed):
    db, settings, archives, _ = indexed
    counters = Indexer(settings, db).run(archives, only_new=True)
    assert counters.extraits == 0 and counters.ignores == counters.detectes
    moved = archives / "2015-2016"
    moved.rename(archives.parent / "deplace")
    try:
        orphans = check_integrity(db)
        assert any("2015-2016" in o for o in orphans)
    finally:
        (archives.parent / "deplace").rename(moved)


# ------------------------------------------------- formats réels FMSP (données anonymisées)
NOUVEAU = """REPUBLIQUE DU CAMEROUN
UNIVERSITE DE DOUALA UNIVERSITY OF DOUALA
FACULTE DE MEDECINE ET DES SCIENCES PHARMACEUTIQUES
RELEVE DE NOTES /TRANSCRIPT
N OM ET PRENOM: ETUDIANT TEST ALPHA MATRICULE: 98MM108
su rname and name: Registration N°:
A AWAE
NÉ(E) LE : 02\\01\\1997
CYCLE: LICENCE ANNÉE ACADÉMIQUE: 2022 - 2023 FILIÈRE: MEDECINE
N IVEAU: I SEMESTRE: II
CODE UNITE D’ENSEIGNEMENT ELEMENT CONSTITUTIF NOTE/20 MOYENNE CREDIT
UE M141 GENETIQUE MEDICALE GENETIQUE MEDICALE 10,00 10,00 2
RELEVE NIVEAU SEMESTRE TOTAL CREDIT / 30 MOYENNE SEMESTRIELLE / 20 MGP GRADE DECISION DU JURY
1 2 24 9,48 1,7 C- SEMESTRE NON VALIDE
Grade Note/4 Appréciation Moy /20
A + 4.0 Excellent [18-20]
"""

LSB = """RELEVE DE NOTES / TRANSCRIPT
NOM(S) ET PRENOM(S): ETUDIANTE TEST BETA GAMMA MATRICULE: 98LSB111
surname and name: Registration N°:
NÉ(E) LE: 22/08/2006 A: VILLE
CYCLE: LICENCE ANNÉE ACADÉMIQUE: 2024-2025 FILIÈRE: SCIENCES BIOMÉDICALES
NIVEAU: 2 SEMESTRE: 3
UE ANATOMIE SYSTEMIQUE 3 14,80
RELEVE TOTAL
SEMESTRE MOYENNE SEMESTRIELLE / 20 MGP GRADE DECISION DU JURY
NIVEAU CREDIT
2 3 30 12,31 2,7 B- SEMESTRE VALIDE
"""

ANCIEN = """RELEVE DE NOTES /TRANSCRIPT
NOM ET PRENOM : ETUDIANT ANCIEN DELTA MATRICULE: 98EM104
Surname and name: Registration N°:
NÉ(E) LE : 18/11/1992 A : VILLE
NIVEAU: III ANNÉE ACADÉMIQUE: 2013 - 2014 FILIÈRE: MEDECINE
Level: Academic Year VI Field of Study
CYCLE : LICENCE SEMESTRE:
Training cycle: semester
N° UNITE ELEMENT CONSTITUTIF NOTE/20 MOYENNE GRADE CREDIT JURY
24 ANATOMIE PATHOLOGIE 11,50 11,50 C+ 8 VALIDEE
25 SEMIOLOGIE 8,20 8,20 D 0 NON VALIDEE
"""

PAGE_VIDE = """RELEVE DE NOTES /TRANSCRIPT FMSP/VDAAP/VDSSSE/SSSE
Il n’est délivré qu’un seul exemplaire de reléve de note , le titulaire peut en faire des copies."""


def test_format_recent_recap_et_libelles_coupes():
    rels, warns = parse_pages([_page(1, NOUVEAU)], r"X:\Archives\Releve_X_EM1_S2_22-23.pdf")
    r = rels[0]
    assert (r["matricule"], r["nom_complet"]) == ("98MM108", "ETUDIANT TEST ALPHA")
    assert (r["niveau"], r["semestre"], r["annee_academique"]) == ("Niveau 1", 2, "2022-2023")
    assert (r["moyenne"], r["credits"], r["grade"], r["mgp"]) == ("9,48", 24, "C-", "1,7")
    assert (r["statut"], r["decision"]) == ("NON VALIDÉ", "SEMESTRE NON VALIDE")
    assert r["date_naissance"] == "02/01/1997" and r["filiere_code"] == "M"


def test_matricule_lsb_et_sciences_biomedicales():
    r = parse_pages([_page(1, LSB)], "x.pdf")[0][0]
    assert (r["matricule"], r["filiere_code"], r["niveau"], r["semestre"]) == ("98LSB111", "B", "Niveau 2", 3)
    assert r["statut"] == "VALIDÉ" and r["moyenne"] == "12,31"


def test_ancien_format_jury_par_ue_et_semestre_decale():
    r = parse_pages([_page(1, ANCIEN)], r"X:\EM3_S2.docx")[0][0]
    assert (r["niveau"], r["semestre"]) == ("Niveau 3", 6)
    assert r["statut"] == "NON VALIDÉ" and "1 UE non validée" in r["decision"]


def test_fichier_collectif_page_vide_et_doublon_avec_entete():
    pages = [_page(1, NOUVEAU), _page(2, NOUVEAU), _page(3, PAGE_VIDE)]
    rels, warns = parse_pages(pages, "collectif.pdf")
    # Même matricule mais en-tête complet -> 2 relevés distincts ; la page 3 vide n'est pas rattachée
    assert [(r["numero_page"], r["page_fin"]) for r in rels] == [(1, 1), (2, 2)]
    assert any("page sans notes" in w for w in warns)


def test_roman_et_matricules_longs():
    from core.normalize import filiere_code_from_matricule, roman_or_int
    assert [roman_or_int(x) for x in ("I", "IV", "VI", "XII", "3")] == [1, 4, 6, 12, 3]
    assert find_matricules("MATRICULE :98MP110 et 98LSB111") == ["98MP110", "98LSB111"]
    assert [filiere_code_from_matricule(m) for m in ("98MM109", "98MP110", "98EM101", "98LSB111")] == ["M", "P", "M", "B"]
