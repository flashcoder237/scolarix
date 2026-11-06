"""Corrections de texte des relevés Word (en-têtes périmés)."""
from __future__ import annotations

import hashlib
import zipfile

import pytest
from docx import Document
from docx.shared import Pt

from core.config import DEFAULT_TEXT_RULES, Settings
from core.text_rules import RuleError, apply_text, compile_rules, rewrite_docx, rules_digest

DEFAULTS = compile_rules(DEFAULT_TEXT_RULES)


def make_releve_docx(path, matricule="98EM101", nom="ETUDIANT TEST", tel_sep="."):
    """Relevé Word fictif dont l'en-tête est découpé en plusieurs « runs », comme dans les vrais fichiers."""
    doc = Document()
    header = doc.sections[0].header
    p = header.paragraphs[0]
    p.add_run("FACULTE DE MEDECINE ET DES SCIENCES PHARMACEUTIQUES")
    p2 = header.add_paragraph()
    for chunk in ("Tel  : 33 01 41 ", "39  / 33 01 41 29", f"{tel_sep}     B.P. 2701. e-mail : facmedouala@gmail.com"):
        p2.add_run(chunk)
    p3 = header.add_paragraph()
    for text, size in (("                ", 16), ("RELEVE DE NOTES", 16), (" /TRANSCRIPT", 16), ("        ", 16),
                       ("FMSP/VDSS", 9), ("SE", 9), ("/SS", 9)):
        r = p3.add_run(text)
        r.font.size = Pt(size)
    doc.add_paragraph(f"NOM ET PRENOM: {nom}                MATRICULE : {matricule}")
    doc.add_paragraph("NÉ(E) LE : 29/03/1990          A : DOUALA")
    doc.add_paragraph("NIVEAU: III          ANNÉE ACADÉMIQUE: 2013 - 2014          FILIÈRE: MEDECINE")
    doc.add_paragraph("CYCLE : LICENCE          SEMESTRE: V")
    t = doc.add_table(rows=2, cols=6)
    for i, v in enumerate(("19", "IMMUNOPATHOLOGIE", "14,50", "B+", "4", "VALIDEE")):
        t.cell(0, i).text = v
    for i, v in enumerate(("20", "HEMATOLOGIE", "12,40", "B", "4", "VALIDEE")):
        t.cell(1, i).text = v
    doc.save(str(path))
    return path


def header_text(path) -> str:
    import re
    with zipfile.ZipFile(path) as z:
        xml = "".join(z.read(n).decode("utf-8") for n in z.namelist() if n.startswith("word/header"))
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))


def test_regles_par_defaut_sur_du_texte():
    text, n = apply_text("Tel  : 33 01 41 39  / 33 01 41 29.     B.P. 2701   RELEVE DE NOTES /TRANSCRIPT    "
                         "FMSP/VDAAP/VDSSSE/SSSE", DEFAULTS)
    assert n == 3
    assert "33 01 41" not in text and "B.P. 2701" in text
    assert "Ref N° RELEVE DE NOTES /TRANSCRIPT" in text
    assert "/FMSP/VDPSAA/VDSSE/VDRC/CDAASSR/SSE" in text and "VDAAP" not in text


@pytest.mark.parametrize("ref", ["FMSP/VDAAP/VDSSSE/SSSE", "FMSP/DVSSSE/SSS", "FMSP/VDSSSE/SS",
                                 "FMSP/VDPSAA/VDSSE/VDRC/SSSE"])
def test_toutes_les_anciennes_references(ref):
    text, _ = apply_text(f"TRANSCRIPT   {ref}", DEFAULTS)
    assert text.strip().endswith("/FMSP/VDPSAA/VDSSE/VDRC/CDAASSR/SSE")


def test_ref_n_pas_ajoute_deux_fois():
    text, n = apply_text("Ref N° RELEVE DE NOTES /TRANSCRIPT", compile_rules([DEFAULT_TEXT_RULES[1]]))
    assert n == 0 and text.count("Ref N°") == 1


def test_texte_simple_tolere_les_espaces_et_echappe_le_remplacement():
    rules = compile_rules([{"actif": True, "regex": False, "chercher": "Tel : 33", "remplacer": r"Tél.\1"}])
    text, n = apply_text("Tel   :   33 01", rules)
    assert n == 1 and text == r"Tél.\1 01"


def test_regles_inactives_vides_et_invalides():
    assert compile_rules([{"actif": False, "chercher": "x", "remplacer": "y"},
                          {"actif": True, "chercher": "  ", "remplacer": "y"}]) == []
    with pytest.raises(RuleError):
        compile_rules([{"actif": True, "regex": True, "chercher": "(", "remplacer": ""}])


def test_empreinte_des_regles():
    assert rules_digest(DEFAULT_TEXT_RULES, False) == "brut"
    assert rules_digest([], True) == "brut"
    d1 = rules_digest(DEFAULT_TEXT_RULES, True)
    changed = [dict(r) for r in DEFAULT_TEXT_RULES]
    changed[2]["remplacer"] = " /FMSP/AUTRE"
    assert d1 != rules_digest(changed, True)
    # la description n'influe pas : renommer une règle ne refait pas les aperçus
    renamed = [dict(r, description="x") for r in DEFAULT_TEXT_RULES]
    assert d1 == rules_digest(renamed, True)


def test_docx_corrige_sur_une_copie_runs_decoupes(tmp_path):
    src = make_releve_docx(tmp_path / "releve.docx")
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    out = tmp_path / "copie.docx"
    n = rewrite_docx(src, out, DEFAULTS)
    assert n == 3
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before        # original intact
    h = header_text(out)
    assert "33 01 41" not in h and "B.P. 2701" in h                       # « Tel » retiré malgré 2 runs
    assert "Ref N° RELEVE DE NOTES /TRANSCRIPT" in h
    assert "/FMSP/VDPSAA/VDSSE/VDRC/CDAASSR/SSE" in h and "VDSS" not in h.replace("VDSSE/VDRC", "")
    doc = Document(str(out))
    runs = doc.sections[0].header.paragraphs[2].runs
    ref = next(r for r in runs if "Ref N°" in r.text)
    assert ref.font.size == Pt(8) and not ref.font.bold                  # « Ref N° » en petit, pas en titre
    titre = next(r for r in runs if "RELEVE DE NOTES" in r.text)
    assert titre.font.size == Pt(16)                                      # le titre garde sa taille
    code = next(r for r in runs if "CDAASSR" in r.text)
    assert code.font.size == Pt(7)
    # la ligne s'est allongée : des espaces de mise en page ont été retirés pour compenser
    assert sum(len(r.text) for r in runs if not r.text.strip()) < 24


def test_docx_corps_du_document_non_touche(tmp_path):
    src = make_releve_docx(tmp_path / "r.docx")
    out = tmp_path / "o.docx"
    rewrite_docx(src, out, DEFAULTS)
    body = [p.text for p in Document(str(out)).paragraphs]
    assert body == [p.text for p in Document(str(src)).paragraphs]


def test_docx_sans_regle_copie_identique(tmp_path):
    src = make_releve_docx(tmp_path / "r.docx")
    out = tmp_path / "o.docx"
    assert rewrite_docx(src, out, []) == 0
    assert out.read_bytes() == src.read_bytes()


def test_regles_par_defaut_dans_la_configuration():
    s = Settings()
    assert s.text_rules_enabled and len(s.text_rules) == 3
    s.text_rules[0]["actif"] = False                     # copie : ne modifie pas les valeurs par défaut
    assert DEFAULT_TEXT_RULES[0]["actif"] is True


def _rpr_order_ok(path) -> bool:
    """Les propriétés de chaque run respectent l'ordre du schéma OOXML (sinon Word refuse le fichier)."""
    from lxml import etree

    from core.text_rules import _RPR_ORDER, W
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.startswith("word/") or not name.endswith(".xml"):
                continue
            root = etree.fromstring(z.read(name))
            for rpr in root.iter(W + "rPr"):
                ranks = [_RPR_ORDER.index(c.tag.replace(W, "")) for c in rpr if c.tag.replace(W, "") in _RPR_ORDER]
                if ranks != sorted(ranks):
                    return False
    return True


def test_police_taille_gras_italique_du_texte_ajoute(tmp_path):
    src = make_releve_docx(tmp_path / "r.docx")
    rules = compile_rules([{"actif": True, "regex": False, "chercher": "RELEVE DE NOTES",
                            "remplacer": "Ref N° RELEVE DE NOTES", "police": "Times New Roman", "taille": 10,
                            "gras": True, "italique": True}])
    out = tmp_path / "o.docx"
    assert rewrite_docx(src, out, rules) == 1
    runs = Document(str(out)).sections[0].header.paragraphs[2].runs
    ref = next(r for r in runs if r.text.startswith("Ref N°"))
    assert (ref.font.name, ref.font.size, ref.font.bold, ref.font.italic) == ("Times New Roman", Pt(10), True, True)
    titre = next(r for r in runs if r.text == "RELEVE DE NOTES")
    assert titre.font.size == Pt(16) and titre.font.name != "Times New Roman"   # texte conservé intact
    assert _rpr_order_ok(out)


def test_style_sur_tout_le_resultat_meme_texte(tmp_path):
    """Changer seulement la police / la taille d'un texte existant (remplacement identique)."""
    src = make_releve_docx(tmp_path / "r.docx")
    rules = compile_rules([{"actif": True, "regex": False, "chercher": "RELEVE DE NOTES /TRANSCRIPT",
                            "remplacer": "RELEVE DE NOTES /TRANSCRIPT", "police": "Arial", "taille": 14,
                            "gras": False, "style_sur": "tout"}])
    out = tmp_path / "o.docx"
    assert rewrite_docx(src, out, rules) == 1
    runs = Document(str(out)).sections[0].header.paragraphs[2].runs
    titre = next(r for r in runs if "TRANSCRIPT" in r.text)
    assert titre.text.strip() == "RELEVE DE NOTES /TRANSCRIPT"
    assert (titre.font.name, titre.font.size, titre.font.bold) == ("Arial", Pt(14), False)
    assert _rpr_order_ok(out)


def test_regles_par_defaut_respectent_le_schema(tmp_path):
    src = make_releve_docx(tmp_path / "r.docx")
    out = tmp_path / "o.docx"
    rewrite_docx(src, out, DEFAULTS)
    assert _rpr_order_ok(out)
