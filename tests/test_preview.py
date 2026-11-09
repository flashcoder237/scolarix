"""Aperçu PDF des relevés Word produit à la demande (et corrections d'en-tête)."""
from __future__ import annotations

import hashlib
import io

import pytest
from conftest import HAS_CONVERTER, make_settings
from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from test_text_rules import make_releve_docx

import core.preview as preview
from core.database import Database
from core.extractors import ExtractionError, docx_pages_text, pdf_pages_text
from core.indexer import Indexer


class FakeConverter:
    """Remplace Word/LibreOffice : une page PDF par page du .docx, en-tête compris."""
    available = True
    calls: list = []

    def __init__(self, settings=None, alter=None):
        self.alter = alter

    def convert(self, src, dest, fmt="pdf", label=None):
        FakeConverter.calls.append((str(src), fmt, label))
        assert fmt == "pdf"
        head = [p.text for p in Document(str(src)).sections[0].header.paragraphs]
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        for page in docx_pages_text(src):
            y = 800
            for line in head + page.text.splitlines():
                if self.alter:
                    line = self.alter(line)
                c.drawString(30, y, line[:110])
                y -= 14
            c.showPage()
        c.save()
        dest.write_bytes(buf.getvalue())

    def close(self):
        pass

    def kill(self):
        pass


def two_releves_docx(path):
    """Deux relevés dans un même fichier Word (fichier collectif), séparés par un saut de page."""
    make_releve_docx(path, "98EM101", "PREMIER ETUDIANT")
    doc = Document(str(path))
    doc.add_page_break()
    doc.add_paragraph("NOM ET PRENOM: SECOND ETUDIANT                MATRICULE : 98EM102")
    doc.add_paragraph("NÉ(E) LE : 02/05/1993          A : DOUALA")
    doc.add_paragraph("NIVEAU: III          ANNÉE ACADÉMIQUE: 2013 - 2014          FILIÈRE: MEDECINE")
    doc.add_paragraph("CYCLE : LICENCE          SEMESTRE: V")
    doc.add_paragraph("21  PHARMACOLOGIE  11,40  C+  5  VALIDEE")
    doc.save(str(path))
    return path


@pytest.fixture()
def word_index(tmp_path, data_dir, monkeypatch):
    archives = tmp_path / "archives"
    archives.mkdir()
    src = two_releves_docx(archives / "EM3_S1.docx")
    settings = make_settings(tmp_path)
    db = Database(settings.db_path, cache_dir=settings.resolved_cache_dir())
    Indexer(settings, db).run(archives, only_new=False, workers=1)
    monkeypatch.setattr(preview, "WordConverter", FakeConverter)
    FakeConverter.calls = []
    return db, settings, src


def test_docx_indexe_sans_conversion(word_index):
    db, settings, _ = word_index
    rels = db.search("98EM")
    assert {r.matricule for r in rels} == {"98EM101", "98EM102"}
    assert all(r.type_fichier == "DOCX" and r.cache_pdf is None for r in rels)
    svc = preview.PreviewService(db, settings)
    assert all(svc.needs_render(r) for r in rels)


def test_apercu_produit_a_la_demande_avec_corrections(word_index):
    db, settings, src = word_index
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    svc = preview.PreviewService(db, settings)
    rel = db.search("98EM102")[0]
    out = svc.ensure(rel)
    assert out.preview_pdf and out.preview_pdf.endswith(".pdf")
    assert preview.preview_name(rel.chemin, settings) in out.preview_pdf
    pages = pdf_pages_text(out.preview_pdf)
    assert "98EM102" in pages[out.numero_page - 1].text            # la bonne page, celle du bon étudiant
    assert "Ref N°" in pages[0].text and "33 01 41" not in pages[0].text
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before  # original jamais modifié
    # le fichier entier est traité une fois : l'autre relevé est prêt sans nouvelle conversion
    other = db.search("98EM101")[0]
    assert not svc.needs_render(other)
    assert svc.ensure(other).preview_pdf == out.preview_pdf
    assert len(FakeConverter.calls) == 1
    svc.close()


def test_modifier_une_regle_refait_l_apercu(word_index):
    db, settings, _ = word_index
    svc = preview.PreviewService(db, settings)
    rel = svc.ensure(db.search("98EM101")[0])
    first = rel.preview_pdf
    settings.text_rules = [dict(r) for r in settings.text_rules]
    settings.text_rules[2]["remplacer"] = " /FMSP/NOUVELLE/REF"
    assert svc.needs_render(rel)
    rel2 = svc.ensure(rel)
    assert rel2.preview_pdf != first
    assert "/FMSP/NOUVELLE/REF" in pdf_pages_text(rel2.preview_pdf)[0].text
    settings.text_rules_enabled = False
    rel3 = svc.ensure(rel2)
    assert "FMSP/VDSSSE/SS" in pdf_pages_text(rel3.preview_pdf)[0].text   # corrections désactivées : texte d'origine
    svc.close()


def test_releve_introuvable_apres_rendu_bloque_l_impression(word_index, monkeypatch):
    db, settings, _ = word_index

    class Altering(FakeConverter):
        def __init__(self, settings=None):
            super().__init__(settings, alter=lambda line: line.replace("98EM102", "11EM999"))
    monkeypatch.setattr(preview, "WordConverter", Altering)
    svc = preview.PreviewService(db, settings)
    with pytest.raises(ExtractionError, match="98EM102"):
        svc.ensure(db.search("98EM102")[0])
    assert any("introuvable" in a["message"] for a in db.anomalies())
    svc.close()


def test_sans_convertisseur_message_clair(word_index, monkeypatch):
    db, settings, _ = word_index

    class Missing(FakeConverter):
        available = False
    monkeypatch.setattr(preview, "WordConverter", Missing)
    svc = preview.PreviewService(db, settings)
    with pytest.raises(ExtractionError, match="Word ni LibreOffice"):
        svc.ensure(db.search("98EM101")[0])
    svc.close()


def test_pdf_n_a_jamais_besoin_de_rendu(indexed):
    db, settings = indexed[0], indexed[1]
    svc = preview.PreviewService(db, settings)
    r = next(r for r in db.search("18M042") if r.type_fichier.startswith("PDF"))
    assert not svc.needs_render(r) and svc.ensure(r) is r
    svc.close()


@pytest.mark.skipif(not HAS_CONVERTER, reason="LibreOffice non installé")
def test_rendu_reel_libreoffice(tmp_path, data_dir):
    archives = tmp_path / "a"
    archives.mkdir()
    two_releves_docx(archives / "collectif.docx")
    settings = make_settings(tmp_path)
    db = Database(settings.db_path, cache_dir=settings.resolved_cache_dir())
    Indexer(settings, db).run(archives, only_new=False, workers=1)
    svc = preview.PreviewService(db, settings)
    rel = svc.ensure(db.search("98EM102")[0])
    pages = pdf_pages_text(rel.preview_pdf)
    assert "98EM102" in pages[rel.numero_page - 1].text
    assert "Ref N°" in pages[0].text and "33 01 41" not in pages[0].text
    assert "CDAASSR" in pages[0].text
    svc.close()
