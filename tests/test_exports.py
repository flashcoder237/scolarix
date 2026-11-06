"""Dossier des exports : classement, non-écrasement, registre des délivrances, conservation."""
from __future__ import annotations

import os
import time
from datetime import date

from conftest import make_settings
from pypdf import PdfReader

from core import exports, pdf_tools
from core.database import Database


def test_classement_par_nature_et_par_mois(tmp_path):
    s = make_settings(tmp_path, exports_root=str(tmp_path / "Exports"))
    d = s.export_dir(exports.DUPLICATAS)
    assert d == tmp_path / "Exports" / "Duplicatas" / f"{date.today():%Y}" / f"{date.today():%m}"
    assert d.is_dir()


def test_jamais_d_ecrasement(tmp_path):
    s = make_settings(tmp_path, exports_root=str(tmp_path / "E"))
    a = exports.dest_for(s, exports.RELEVES, "FMSP_98EM101_X.pdf")
    a.write_bytes(b"1")
    b = exports.dest_for(s, exports.RELEVES, "FMSP_98EM101_X.pdf")
    assert a != b and not b.exists() and b.name.startswith("FMSP_98EM101_X_")


def test_racine_par_defaut_hors_du_bureau(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / "Documents").mkdir()
    s = make_settings(tmp_path)
    assert s.exports_root_dir() == tmp_path / "Documents" / "Exports Scolarix"


def test_numeros_de_delivrance_et_empreinte(tmp_path, data_dir):
    s = make_settings(tmp_path)
    db = Database(s.db_path)
    r1 = db.new_delivery("IMPRESSION", "G1", "agent", "98EM101", year=2026)
    r2 = db.new_delivery("EXPORT PDF", "G1", "agent", "98EM101", year=2026)
    r3 = db.new_delivery("EXPORT PDF", "G2", "agent", "98EM102", year=2026)
    assert (r1, r2, r3) == ("00001/G1/2026", "00002/G1/2026", "00001/G2/2026")
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF contenu")
    db.complete_delivery(r2, str(f), exports.sha256_file(f))
    row = db.find_delivery(exports.sha256_file(f))
    assert row["reference"] == r2 and row["matricule"] == "98EM101"
    f.write_bytes(b"%PDF contenu modifie")
    assert db.find_delivery(exports.sha256_file(f)) is None
    assert [r["reference"] for r in db.deliveries("98EM101")] == [r2, r1]


def test_pied_de_page_reference(indexed):
    db = indexed[0]
    rel = next(r for r in db.search("18M042") if r.type_fichier.startswith("PDF"))
    out = pdf_tools.extract_pages(rel.preview_pdf, rel.numero_page, rel.page_fin,
                                  footer=pdf_tools.footer_text("00042/G1/2026"))
    assert "00042/G1/2026" in PdfReader(str(out)).pages[0].extract_text()
    pdf_tools.purge_temp()


def test_conservation_par_nature(tmp_path):
    s = make_settings(tmp_path, exports_root=str(tmp_path / "E"), exports_retention_days=30)
    old = time.time() - 40 * 86400
    keep, purge = [], []
    for nature in (exports.RELEVES, exports.CURSUS, exports.VERIFICATIONS, exports.DUPLICATAS,
                   exports.EXTRACTIONS, exports.JOURNAL):
        f = s.export_dir(nature) / "ancien.pdf"
        f.write_bytes(b"x")
        os.utime(f, (old, old))
        (purge if nature in exports.PURGEABLE else keep).append(f)
    recent = s.export_dir(exports.RELEVES) / "recent.pdf"
    recent.write_bytes(b"x")
    assert exports.purge_old(s) == 3
    assert all(not f.exists() for f in purge) and all(f.exists() for f in keep) and recent.exists()
    s.exports_retention_days = 0
    assert exports.purge_old(s) == 0
