"""Fidélité des aperçus Word corrigés (2.0.5) — documents FICTIFS uniquement.

Constat au guichet : avec les corrections actives, l'en-tête de certains .doc était abîmé (cadre vide, référence
rouge tronquée, titres français / anglais superposés) ; sans corrections, l'aperçu était parfait.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from conftest import HAS_CONVERTER, make_settings
from docx import Document
from docx.shared import Pt
from lxml import etree
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from test_preview import FakeConverter, two_releves_docx
from word_samples import REF, make_letterhead_docx

import core.preview as preview
from core import layout_check
from core.config import DEFAULT_TEXT_RULES
from core.text_rules import (W, RewriteReport, RuleError, compile_rules, parse_line_spacing, rewrite_docx,
                             rule_warnings, rules_digest)

DEFAULTS = compile_rules(DEFAULT_TEXT_RULES)
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
NEW_REF = "/FMSP/VDPSAA/VDSSE/VDRC/CDAASSR/SSE"


def header_root(path: Path):
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.startswith("word/header"))
        return etree.fromstring(z.read(name))


def texts(el) -> str:
    return "".join(t.text or "" for t in el.iter(W + "t"))


def sizes_of(el, needle: str) -> list[float]:
    return [int(r.find(f"{W}rPr/{W}sz").get(W + "val")) / 2 for r in el.iter(W + "r")
            if needle in texts(r) and r.find(f"{W}rPr/{W}sz") is not None]


@pytest.fixture()
def letterhead(tmp_path):
    return make_letterhead_docx(tmp_path / "entete.docx")


# ------------------------------------------------------------------ zones de texte (mc:AlternateContent)
def test_zone_de_texte_choice_et_fallback_identiques(letterhead, tmp_path):
    out = tmp_path / "o.docx"
    rep = RewriteReport()
    assert rewrite_docx(letterhead, out, DEFAULTS, rep) == 3
    root = header_root(out)
    ac = next(root.iter(MC + "AlternateContent"))
    choice = next(ac.find(MC + "Choice").iter(W + "txbxContent"))
    fallback = next(ac.find(MC + "Fallback").iter(W + "txbxContent"))
    assert texts(choice) == texts(fallback) == NEW_REF          # pas d'espace de tête : 1re ligne jamais vide
    assert [etree.tostring(c) for c in choice] == [etree.tostring(c) for c in fallback]
    # le texte ajouté a été réduit pour tenir sur UNE ligne dans la zone de taille fixe (135 pt)
    assert max(sizes_of(choice, "CDAASSR")) < 7
    # la zone elle-même (taille, position, ancrage) n'a pas changé
    src = header_root(letterhead)
    for tag in ("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}anchor",
                "{urn:schemas-microsoft-com:vml}shape"):
        a, b = next(src.iter(tag)), next(root.iter(tag))
        assert dict(a.attrib) == dict(b.attrib)


def test_zone_trop_petite_correction_ecartee(tmp_path):
    from word_samples import _DECL, textbox
    doc = Document()
    hdr = doc.sections[0].header._element
    frag = etree.fromstring(f'<w:p {_DECL}>' + textbox(REF, 200, 60, 135, 12, 1) + "</w:p>")
    hdr.append(frag)
    doc.add_paragraph("NOM ET PRENOM: ETUDIANT FICTIF   MATRICULE : 99EM001")
    src, out = tmp_path / "petit.docx", tmp_path / "o.docx"
    doc.save(str(src))
    rep = RewriteReport()
    longer = "SERVICE CENTRAL FICTIF DE LA SCOLARITE, DES ARCHIVES ET DES RELEVES DE NOTES"
    rules = compile_rules([{"actif": True, "chercher": REF, "remplacer": longer}])
    assert rewrite_docx(src, out, rules, rep) == 0                  # même à 5 pt, le texte ne tiendrait pas
    assert REF in texts(header_root(out)) and longer not in texts(header_root(out))
    assert any("ne tient pas" in s for s in rep.skipped)


def test_aucune_regle_applicable_aucun_changement_de_structure(letterhead, tmp_path):
    out = tmp_path / "o.docx"
    rules = compile_rules([{"actif": True, "chercher": "TEXTE ABSENT DU DOCUMENT", "remplacer": "X", "taille": 20}])
    assert rewrite_docx(letterhead, out, rules) == 0
    zi, zo = zipfile.ZipFile(letterhead), zipfile.ZipFile(out)
    assert zi.namelist() == zo.namelist()
    for n in zi.namelist():
        if n != "word/settings.xml":
            assert zi.read(n) == zo.read(n), n


# ------------------------------------------------------------------ polices symbole, champs
def test_runs_en_police_symbole_jamais_touches(tmp_path):
    doc = Document()
    p = doc.sections[0].header.paragraphs[0]
    for text, font in (("TITRE", None), ("(", "Wingdings"), ("FMSP/VDAAP", "Wingdings")):
        r = p.add_run(text)
        if font:
            r.font.name = font
    src, out = tmp_path / "s.docx", tmp_path / "o.docx"
    doc.save(str(src))
    rep = RewriteReport()
    # (1) correspondance DANS un run symbole : non modifiée
    assert rewrite_docx(src, out, DEFAULTS, rep) == 0 and any("symbole" in s for s in rep.skipped)
    # (2) insertion à la fin d'un texte suivi d'un run symbole : le texte ajouté ne prend pas la police symbole
    rules = compile_rules([{"actif": True, "chercher": "TITRE", "remplacer": "TITRE COMPLET"}])
    assert rewrite_docx(src, out, rules) == 1
    for r in header_root(out).iter(W + "r"):
        if "COMPLET" in texts(r):
            rf = r.find(f"{W}rPr/{W}rFonts")
            assert rf is None or "Wingdings" not in (rf.get(W + "ascii") or "")


def test_texte_d_un_champ_jamais_touche(tmp_path):
    doc = Document()
    p = doc.sections[0].header.paragraphs[0]._p
    frag = ('<w:r xmlns:w="%s"><w:fldChar w:fldCharType="begin"/></w:r><w:r xmlns:w="%s"><w:instrText> TITLE '
            '</w:instrText></w:r><w:r xmlns:w="%s"><w:fldChar w:fldCharType="separate"/></w:r><w:r xmlns:w="%s">'
            '<w:t>FMSP/VDAAP/SSSE</w:t></w:r><w:r xmlns:w="%s"><w:fldChar w:fldCharType="end"/></w:r>') % ((W[1:-1],) * 5)
    for el in etree.fromstring(f"<x>{frag}</x>"):
        p.append(el)
    src, out = tmp_path / "f.docx", tmp_path / "o.docx"
    doc.save(str(src))
    assert rewrite_docx(src, out, DEFAULTS) == 0
    assert "FMSP/VDAAP/SSSE" in texts(header_root(out))


# ------------------------------------------------------------------ mise en forme du texte conservé
def test_ref_n_seul_le_texte_ajoute_est_reduit(letterhead, tmp_path):
    """« RELEVE DE NOTES / TRANSCRIPT » (espace après la barre) : avant 2.0.5, « R » restait en 14 pt et
    « ef N° RELEVE DE NOTES / » passait en 8 pt."""
    out = tmp_path / "o.docx"
    rewrite_docx(letterhead, out, DEFAULTS)
    title = next(p for p in header_root(out).iter(W + "p") if "TRANSCRIPT" in texts(p))
    assert texts(title) == "Ref N° RELEVE DE NOTES /TRANSCRIPT"
    runs = [(texts(r), int(r.find(f"{W}rPr/{W}sz").get(W + "val")) / 2) for r in title.iter(W + "r") if texts(r)]
    assert ("Ref N° ", 8.0) in runs
    assert all(size == 14 for text, size in runs if text.strip() and "Ref" not in text)


def test_ligne_raccourcie_garde_sa_largeur(letterhead, tmp_path):
    """Téléphone retiré d'une ligne de 2 lignes : la largeur est rendue (sinon tout le flux remonte et les titres
    ancrés à la page se superposent)."""
    out = tmp_path / "o.docx"
    rewrite_docx(letterhead, out, DEFAULTS)
    line = next(p for p in header_root(out).iter(W + "p") if "B.P. 2701" in texts(p))
    assert "33 01 41" not in texts(line) and "  " * 10 in texts(line)


# ------------------------------------------------------------------ interligne / espaces des règles
def test_interligne_et_espaces_seulement_sur_les_paragraphes_modifies(tmp_path):
    from test_text_rules import make_releve_docx
    src = make_releve_docx(tmp_path / "r.docx")
    rule = dict(DEFAULT_TEXT_RULES[1], interligne={"exact": 18}, espace_avant=3, espace_apres=0)
    out = tmp_path / "o.docx"
    assert rewrite_docx(src, out, compile_rules([rule])) == 1
    paras = list(header_root(out).iter(W + "p"))
    spaced = [p for p in paras if p.find(f"{W}pPr/{W}spacing") is not None]
    assert len(spaced) == 1 and "Ref N°" in texts(spaced[0])
    sp = spaced[0].find(f"{W}pPr/{W}spacing")
    assert (sp.get(W + "line"), sp.get(W + "lineRule"), sp.get(W + "before"), sp.get(W + "after")) == \
        ("360", "exact", "60", "0")
    ppr = [etree.QName(c).localname for c in spaced[0].find(W + "pPr")]
    if "jc" in ppr:
        assert ppr.index("spacing") < ppr.index("jc")                    # ordre du schéma
    assert Document(str(out)).paragraphs                                    # toujours un .docx valide


def test_interligne_valeurs_et_empreinte():
    assert parse_line_spacing(None) is None and parse_line_spacing("") is None
    assert parse_line_spacing("simple") == (240, "auto")
    assert parse_line_spacing("1,5") == (360, "auto")
    assert parse_line_spacing({"au_moins": 12}) == (240, "atLeast")
    with pytest.raises(RuleError):
        compile_rules([{"chercher": "x", "remplacer": "y", "interligne": "beaucoup"}])
    with pytest.raises(RuleError):
        compile_rules([{"chercher": "x", "remplacer": "y", "espace_avant": -4}])
    base = rules_digest(DEFAULT_TEXT_RULES)
    # les champs vides (règles enregistrées avant 2.0.5) ne changent pas l'empreinte…
    assert base == rules_digest([{k: v for k, v in r.items() if k not in ("interligne", "espace_avant",
                                                                           "espace_apres")}
                                 for r in DEFAULT_TEXT_RULES])
    # … une valeur la change : les aperçus sont refaits
    changed = [dict(r) for r in DEFAULT_TEXT_RULES]
    changed[1]["interligne"] = "1.15"
    assert rules_digest(changed) != base
    changed[1]["interligne"], changed[1]["espace_apres"] = None, 6
    assert rules_digest(changed) != base


def test_texte_agrandi_dans_un_interligne_exact_limite(tmp_path):
    doc = Document()
    p = doc.sections[0].header.paragraphs[0]
    p.paragraph_format.line_spacing = Pt(10)                                # interligne exact de 10 pt
    from docx.enum.text import WD_LINE_SPACING
    p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    p.add_run("RELEVE DE NOTES /TRANSCRIPT").font.size = Pt(9)
    src, out = tmp_path / "e.docx", tmp_path / "o.docx"
    doc.save(str(src))
    rule = dict(DEFAULT_TEXT_RULES[1], taille=14)
    rep = RewriteReport()
    assert rewrite_docx(src, out, compile_rules([rule]), rep) == 1
    assert max(sizes_of(header_root(out), "Ref")) <= 10 / 1.15 + 0.01      # pas de texte rogné
    assert any("interligne exact" in s for s in rep.skipped)
    assert rule_warnings(compile_rules([rule]))                             # mise en garde dans l'essai


# ------------------------------------------------------------------ contrôle des rendus
def _page(lines, images=()):
    """PageLayout fictive : [(texte, x, y, taille)]."""
    lay = layout_check.PageLayout(images=list(images))
    chars = []
    for text, x, y, size in lines:
        for k, ch in enumerate(text):
            if ch.isspace():
                continue
            chars.append(ch)
            lay.boxes.append((x + k * size * 0.5, y, x + (k + 1) * size * 0.5, y + size))
    lay.text = "".join(chars)
    return lay


HEAD = [("FACULTE DE MEDECINE", 100, 700, 11), ("FACULTY OF MEDICINE", 100, 686, 11),
        ("RELEVE DE NOTES /TRANSCRIPT", 100, 660, 14), ("Tel : 33 01 41 39", 100, 640, 9)]


def test_controle_rendu_fidele_et_ecarts():
    reps = [("RELEVE DE NOTES /TRANSCRIPT", "Ref N° RELEVE DE NOTES /TRANSCRIPT"), ("Tel : 33 01 41 39", "")]
    orig = _page(HEAD, [(10, 10, 50, 50)])
    good = _page(HEAD[:2] + [("Ref N° RELEVE DE NOTES /TRANSCRIPT", 80, 660, 14)], [(10, 10, 50, 50)])
    assert layout_check.compare_pages(orig, orig, 1, []) is None
    assert layout_check.compare_pages(orig, good, 1, reps) is None
    # titres superposés : la ligne anglaise remonte de 12 pt
    shifted = _page([HEAD[0], ("FACULTY OF MEDICINE", 100, 698, 11)]
                    + [("Ref N° RELEVE DE NOTES /TRANSCRIPT", 80, 660, 14)], [(10, 10, 50, 50)])
    assert "déplacé" in layout_check.compare_pages(orig, shifted, 1, reps)
    # texte rogné (une partie du remplacement manque)
    clipped = _page(HEAD[:2] + [("Ref N° RELEVE DE NOTES", 80, 660, 14)], [(10, 10, 50, 50)])
    assert "texte différent" in layout_check.compare_pages(orig, clipped, 1, reps)
    # remplacement passé à la ligne suivante
    wrapped = _page(HEAD[:2] + [("Ref N° RELEVE DE NOTES", 80, 660, 14), ("/TRANSCRIPT", 80, 644, 14)],
                    [(10, 10, 50, 50)])
    assert "hauteur" in layout_check.compare_pages(orig, wrapped, 1, reps)
    # logo déplacé
    assert "image" in layout_check.compare_pages(orig, _page(HEAD[:2] + [("Ref N° RELEVE DE NOTES /TRANSCRIPT",
                                                                         80, 660, 14)], [(10, 30, 50, 70)]), 1, reps)


class ShiftingConverter(FakeConverter):
    """Comme Word sur un en-tête fragile : la copie corrigée (nouvelle référence plus longue) passe à la ligne et
    décale tout ce qui suit d'une ligne."""

    def convert(self, src, dest, fmt="pdf", label=None):
        FakeConverter.calls.append((str(src), fmt, label))
        from core.extractors import docx_pages_text
        head = [p.text for p in Document(str(src)).sections[0].header.paragraphs]
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        for page in docx_pages_text(src):
            y = 800
            for line in head + page.text.splitlines():
                c.drawString(30, y, line[:110])
                y -= 28 if "CDAASSR" in line else 14
            c.showPage()
        c.save()
        dest.write_bytes(buf.getvalue())


@pytest.fixture()
def fragile_index(tmp_path, data_dir, monkeypatch):
    from core.database import Database
    from core.indexer import Indexer
    archives = tmp_path / "archives"
    archives.mkdir()
    two_releves_docx(archives / "EM7_S13.docx")
    settings = make_settings(tmp_path)
    db = Database(settings.db_path, cache_dir=settings.resolved_cache_dir())
    Indexer(settings, db).run(archives, only_new=False, workers=1)
    FakeConverter.calls = []
    return db, settings


def test_mise_en_page_modifiee_repli_sur_l_original_et_alerte(fragile_index, monkeypatch):
    from core.extractors import pdf_pages_text
    from core.service import Service
    db, settings = fragile_index
    monkeypatch.setattr(preview, "WordConverter", ShiftingConverter)
    svc = preview.PreviewService(db, settings)
    rel = svc.ensure(db.search("98EM102")[0])
    text = pdf_pages_text(rel.preview_pdf)[0].text
    assert "33 01 41" in text and "CDAASSR" not in text            # l'original, sans aucune correction
    meta = preview.preview_meta(rel.preview_pdf)
    assert meta["repli"] and meta["corrections"] == 0 and meta["prevues"] == 3
    service = Service(settings, svc)
    d = service.releve(rel.id)
    msgs = [a["text"] for a in d["alerts"]]
    assert any(m.startswith("Corrections non appliquées : mise en page à risque") for m in msgs)
    assert not any(m.startswith("En-tête corrigé") for m in msgs)
    assert d["apercu"]["repli"] and not d["apercu"]["original_disponible"]
    svc.close()


def test_apercu_original_a_la_demande(fragile_index, monkeypatch):
    from core.extractors import pdf_pages_text
    from core.service import Service
    db, settings = fragile_index
    monkeypatch.setattr(preview, "WordConverter", FakeConverter)
    svc = preview.PreviewService(db, settings)
    service = Service(settings, svc)
    rel = db.search("98EM102")[0]
    service.preview(rel.id)
    d = service.releve(rel.id)
    assert d["apercu"]["corrections"] == 3 and d["apercu"]["original_disponible"]
    n = len(FakeConverter.calls)
    o = service.preview_original(rel.id)
    assert len(FakeConverter.calls) == n                             # déjà rendu pour le contrôle : réutilisé
    assert o["preview_pdf"] != d["preview_pdf"] and o["preview_pdf"].endswith("_brut.pdf")
    page = pdf_pages_text(o["preview_pdf"])[o["numero_page"] - 1].text
    assert "98EM102" in page and "33 01 41" in page and "Ref N°" not in page
    svc.close()


# ------------------------------------------------------------------ .docx intermédiaire écrit par LibreOffice
class _Conv:
    available = True

    def __init__(self, word: bool):
        self.word_available = word
        self.calls = []

    def convert(self, src, dest, fmt="pdf", label=None):
        self.calls.append(fmt)
        shutil.copyfile(_word_docx, dest)


_word_docx: Path


def _with_app(path: Path, application: str) -> None:
    doc = Document()
    doc.add_paragraph("RELEVE FICTIF")
    doc.save(str(path))
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "docProps/app.xml":
                data = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://'
                        b'schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>'
                        + application.encode() + b"</Application></Properties>")
            zout.writestr(item, data)
    tmp.replace(path)


def test_intermediaire_libreoffice_jamais_repris_quand_word_est_la(tmp_path):
    global _word_docx
    _word_docx = tmp_path / "word.docx"
    _with_app(_word_docx, "Microsoft Office Word")
    src = tmp_path / "releve.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0 fictif")
    cache = tmp_path / "cache"
    cache.mkdir()
    inter = preview.intermediate_docx(str(src), cache)
    _with_app(inter, "LibreOffice/7.6.4.1$Windows_X86_64 LibreOffice_project/fictif")   # écrit par l'indexeur 2.0.1
    assert preview.docx_producer(inter).startswith("LibreOffice")
    # poste sans Word : le .docx de l'indexeur est repris tel quel
    conv = _Conv(word=False)
    preview.ensure_intermediate_docx(str(src), cache, conv)
    assert conv.calls == []
    # poste avec Word : refait par Word, même s'il est signé et à jour
    conv = _Conv(word=True)
    preview.ensure_intermediate_docx(str(src), cache, conv)
    assert conv.calls == ["docx"] and preview.produced_by_word(inter)
    preview.ensure_intermediate_docx(str(src), cache, conv)
    assert conv.calls == ["docx"]                                   # celui de Word, lui, est repris


# ------------------------------------------------------------------ rendu réel (LibreOffice), .docx et .doc
@pytest.mark.skipif(not HAS_CONVERTER, reason="LibreOffice non installé")
@pytest.mark.parametrize("kind", ["DOCX", "DOC"])
def test_rendu_reel_en_tete_complexe_fidele(tmp_path, data_dir, kind):
    from core.extractors import WordConverter, pdf_pages_text
    src = make_letterhead_docx(tmp_path / "entete.docx")
    if kind == "DOC":
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        subprocess.run([soffice, "--headless", "--convert-to", "doc", "--outdir", str(tmp_path), str(src)],
                       capture_output=True, timeout=180)
        src = tmp_path / "entete.doc"
        assert src.exists()
    settings = make_settings(tmp_path, word_com_enabled=False)
    with WordConverter(settings) as conv:
        name, n = preview.render_preview(str(src), kind, settings, conv)
    pdf = settings.resolved_cache_dir() / name
    meta = preview.preview_meta(pdf)
    assert not meta.get("repli"), meta.get("motif")
    assert n == 3 and meta["corrections"] == 3
    text = pdf_pages_text(pdf)[0].text
    assert "Ref N°" in text and "33 01 41" not in text and "CDAASSR" in text
    orig = settings.resolved_cache_dir() / preview.original_name(str(src))
    assert layout_check.compare(orig, pdf, [(REF, NEW_REF)] + [
        ("RELEVE DE NOTES / TRANSCRIPT", "Ref N° RELEVE DE NOTES /TRANSCRIPT"),
        ("Tel : 33 01 41 39 / 33 01 41 29.", "")]) is None
