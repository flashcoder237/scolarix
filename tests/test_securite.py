"""Garde-fous de délivrance (revue experte) : jamais la page d'un autre étudiant, jamais un relevé périmé."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from conftest import make_settings
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import core.preview as preview
from core.database import Database
from core.extractors import ExtractionError, PageText
from core.guard import PrintBlocked, check_before_print
from core.indexer import Exclusions, Indexer, scan_sources
from core.parsing import parse_pages


def releve_pdf(path: Path, students: list[tuple[str, str]], extra_page_for: str | None = None) -> Path:
    """PDF collectif fictif : une page par étudiant (option : une page de suite pour l'un d'eux)."""
    c = canvas.Canvas(str(path), pagesize=A4)
    for nom, mat in students:
        y = 800
        for line in ("RELEVE DE NOTES / TRANSCRIPT", f"NOM ET PRENOM: {nom}   MATRICULE: {mat}",
                     "NÉ(E) LE: 01/01/2000 A: DOUALA", "CYCLE: LICENCE ANNÉE ACADÉMIQUE: 2022-2023 FILIÈRE: MEDECINE",
                     "NIVEAU: I SEMESTRE: I", "UE M111 ANATOMIE 12,50 12,50 6",
                     "RELEVE NIVEAU SEMESTRE TOTAL CREDIT / 30 MOYENNE SEMESTRIELLE / 20 MGP GRADE DECISION DU JURY",
                     "1 1 30 12,50 2,7 B- SEMESTRE VALIDE"):
            c.drawString(30, y, line)
            y -= 16
        c.showPage()
        if extra_page_for == mat:
            c.drawString(30, 800, "UE M112 HISTOLOGIE 11,00 11,00 4")
            c.showPage()
    c.save()
    return path


@pytest.fixture()
def pdf_index(tmp_path, data_dir):
    archives = tmp_path / "archives"
    archives.mkdir()
    src = releve_pdf(archives / "promo.pdf", [("PREMIER TEST", "98EM101"), ("SECOND TEST", "98EM102")])
    s = make_settings(tmp_path, ocr_enabled=False)
    db = Database(s.db_path, cache_dir=s.resolved_cache_dir())
    Indexer(s, db).run(archives, only_new=False, workers=1)
    return db, s, archives, src


def test_impression_autorisee_cas_normal(pdf_index):
    db = pdf_index[0]
    for mat in ("98EM101", "98EM102"):
        check_before_print(db.search(mat)[0])


def test_pdf_remplace_apres_indexation_bloque(pdf_index):
    db, _, _, src = pdf_index
    rel = db.search("98EM101")[0]
    time.sleep(1.1)
    releve_pdf(src, [("SECOND TEST", "98EM102"), ("PREMIER TEST", "98EM101")])     # ordre inversé, sans réindexer
    with pytest.raises(PrintBlocked, match="modifié depuis l'indexation"):
        check_before_print(rel)


def test_matricule_absent_de_la_page_bloque(pdf_index):
    db = pdf_index[0]
    rel = db.search("98EM101")[0]
    rel.numero_page = rel.page_fin = 2              # page d'un autre étudiant (index corrompu, bug…)
    with pytest.raises(PrintBlocked, match="n'apparaît pas"):
        check_before_print(rel)


def test_page_de_suite_d_un_autre_etudiant_bloquee(pdf_index):
    db = pdf_index[0]
    rel = db.search("98EM101")[0]
    rel.page_fin = 2
    with pytest.raises(PrintBlocked, match="autre étudiant"):
        check_before_print(rel)


def test_source_supprimee_ou_pages_hors_document(pdf_index):
    db, _, _, src = pdf_index
    rel = db.search("98EM102")[0]
    rel.page_fin = 9
    with pytest.raises(PrintBlocked, match="absentes"):
        check_before_print(rel)
    src.unlink()
    with pytest.raises(PrintBlocked, match="introuvable"):
        check_before_print(db.search("98EM102")[0])


def test_echec_de_lecture_conserve_les_releves_et_reessaie(pdf_index):
    db, s, archives, src = pdf_index
    assert len(db.search("98EM1")) == 2
    time.sleep(1.1)
    src.write_bytes(b"%PDF-1.4 tronque")                 # fichier abîmé pendant une copie réseau
    Indexer(s, db).run(archives, only_new=True, workers=1)
    rels = db.search("98EM1")
    assert len(rels) == 2                                 # les étudiants ne disparaissent pas du guichet
    with pytest.raises(PrintBlocked):                     # mais leur impression est bloquée
        check_before_print(rels[0])
    releve_pdf(src, [("PREMIER TEST", "98EM101"), ("SECOND TEST", "98EM102")])
    c = Indexer(s, db).run(archives, only_new=True, workers=1)
    assert c.extraits == 1                                # le différentiel relit le fichier en erreur
    check_before_print(db.search("98EM101")[0])


def test_en_tete_sans_matricule_lisible_jamais_rattache():
    p1 = PageText(1, "NOM ET PRENOM: PREMIER TEST MATRICULE: 98EM101\nNIVEAU: I SEMESTRE: I\nUE 12,50\n"
                     "1 1 30 12,50 B- SEMESTRE VALIDE")
    p2 = PageText(2, "NOM ET PRENOM: SECOND TEST MATRICULE: 98EM1O2\nNIVEAU: I SEMESTRE: I\nUE 09,00\n"
                     "1 1 12 9,00 D SEMESTRE NON VALIDE")       # « O » lu à la place de « 0 » par l'OCR
    rels, warns = parse_pages([p1, p2], "scan.pdf")
    first = next(r for r in rels if r["matricule"] == "98EM101")
    assert (first["numero_page"], first["page_fin"]) == (1, 1)
    assert first["statut"] == "VALIDÉ"
    assert any("illisible" in w.lower() for w in warns)


def test_remap_ne_confond_pas_normale_et_rattrapage(tmp_path):
    from types import SimpleNamespace
    pdf = releve_pdf(tmp_path / "r.pdf", [("ETUDIANT TEST", "98EM101")])
    normale = SimpleNamespace(id=1, matricule="98EM101", semestre=1, session="Normale",
                              annee_academique="2022-2023", numero_page=1, moyenne=None, decision=None)
    rattrapage = SimpleNamespace(id=2, matricule="98EM101", semestre=1, session="Rattrapage",
                                 annee_academique="2022-2023", numero_page=2, moyenne=None, decision=None)
    mapping, missing = preview.remap_pages(pdf, [normale, rattrapage], str(tmp_path / "S1_SN.docx"))
    assert len(mapping) <= 1 and len(missing) >= 1     # jamais les deux sur la même page


def test_garde_fou_word_persistant(tmp_path, data_dir, monkeypatch):
    """Un relevé introuvable sur le rendu reste bloqué aux appels suivants (pas seulement au premier)."""
    from test_preview import FakeConverter, two_releves_docx
    archives = tmp_path / "a"
    archives.mkdir()
    two_releves_docx(archives / "EM3_S1.docx")
    s = make_settings(tmp_path)
    db = Database(s.db_path, cache_dir=s.resolved_cache_dir())
    Indexer(s, db).run(archives, only_new=False, workers=1)

    class Altering(FakeConverter):
        def __init__(self, settings=None):
            super().__init__(settings, alter=lambda line: line.replace("98EM102", "98EM999"))
    monkeypatch.setattr(preview, "WordConverter", Altering)
    svc = preview.PreviewService(db, s)
    rel = db.search("98EM102")[0]
    for _ in range(3):
        with pytest.raises(ExtractionError):
            svc.ensure(rel)
        rel = db.search("98EM102")[0]
        assert svc.needs_render(rel) and not rel.page_verifiee
    with pytest.raises(PrintBlocked):
        check_before_print(rel)
    ok = svc.ensure(db.search("98EM101")[0])          # l'autre relevé du fichier reste délivrable
    assert ok.page_verifiee
    check_before_print(ok)
    svc.close()


def test_apercu_refait_si_source_restauree_avec_date_ancienne(tmp_path, data_dir, monkeypatch):
    from test_preview import FakeConverter, two_releves_docx
    archives = tmp_path / "a"
    archives.mkdir()
    src = two_releves_docx(archives / "EM3_S1.docx")
    s = make_settings(tmp_path)
    monkeypatch.setattr(preview, "WordConverter", FakeConverter)
    FakeConverter.calls = []
    conv = FakeConverter()
    preview.render_preview(str(src), "DOCX", s, conv)
    old = src.stat().st_mtime
    preview.render_preview(str(src), "DOCX", s, conv)
    assert len(FakeConverter.calls) == 1                 # inchangé : réutilisé
    from docx import Document
    d = Document(str(src))
    d.add_paragraph("CORRECTION")
    d.save(str(src))
    os.utime(src, (old - 3600, old - 3600))              # restauré avec une date PLUS ANCIENNE
    preview.render_preview(str(src), "DOCX", s, conv)
    assert len(FakeConverter.calls) == 2                 # quand même refait


def test_exclusion_sur_racine_resolue(tmp_path):
    real = tmp_path / "reel"
    (real / "Brouillons").mkdir(parents=True)
    releve_pdf(real / "Brouillons" / "x.pdf", [("A", "98EM101")])
    releve_pdf(real / "ok.pdf", [("B", "98EM102")])
    link = tmp_path / "lecteur_mappe"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("liens symboliques indisponibles")
    rule = str(link / "Brouillons")                     # saisi sur le chemin « mappé »
    root = link.resolve()                               # la racine est résolue par le robot
    kept = {Path(f.path).name for f in scan_sources(root, Exclusions(root, [rule]))}
    assert kept == {"ok.pdf"}


def test_registre_des_instances_word(tmp_path, data_dir):
    from core import extractors
    extractors._write_pids([])
    extractors.register_word_pid(999999)               # processus inexistant
    me = os.getpid()
    extractors._write_pids(extractors._read_pids() + [(os.getpid(), me)])
    extractors.kill_orphan_words()
    rows = extractors._read_pids()
    assert (os.getpid(), me) in rows                   # propriétaire vivant : conservé, jamais tué
    assert all(pid != 999999 for pid, _ in rows)       # disparu : retiré du registre
    extractors._write_pids([])


def test_recherche_noms_avec_trait_d_union(pdf_index, tmp_path):
    db, s, archives, _ = pdf_index
    releve_pdf(archives / "autre.pdf", [("ANNE-SOPHIE ESSAI", "98EM103")])
    Indexer(s, db).run(archives, only_new=True, workers=1)
    assert {r.matricule for r in db.search("annesophie")} == {"98EM103"}


def test_copie_de_base_sur_partage_refusee():
    from core.config import is_network_path
    assert is_network_path(r"\\SRV-FMSP\Archives\index.db")
    assert not is_network_path(str(Path.home() / "index.db"))
    shutil.rmtree(Path.home() / "inexistant", ignore_errors=True)
