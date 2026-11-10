"""Vérification 2.1 : champs situés sur les deux documents (surlignage des divergences), un seul OCR par document.

Documents soumis FICTIFS (tools/verif_samples.py) : relevé N5 de 18M042 avec la note de MED502 et la moyenne retouchées.
"""
from __future__ import annotations

import dataclasses
import shutil
import sys
from pathlib import Path

import pytest

from core.service import Service

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


@pytest.fixture()
def svc(indexed, tmp_path, monkeypatch):
    monkeypatch.setenv("FMSP_DATA_DIR", str(tmp_path / "data"))
    _db, settings, _archives, _ = indexed
    s = dataclasses.replace(settings, exports_root=str(tmp_path / "Exports"), text_rules=list(settings.text_rules),
                            exclusions=list(settings.exclusions))
    return Service(s, previews=None)


@pytest.fixture()
def docs(tmp_path):
    import verif_samples
    return verif_samples.generate(tmp_path / "soumis")


def _n5(svc):
    return next(r for r in svc.cursus_info("18M042")["releves"] if r["annee_academique"] == "2022-2023" and not r["is_word"])


def test_lignes_situees_et_regroupees():
    from core.verification import Ligne, _group_lines, _unrotate
    lignes = _group_lines([(1, 0.5, 0.100, 0.6, 0.112, "14,25"), (1, 0.1, 0.101, 0.3, 0.113, "MED355 Pharmacologie"),
                           (1, 0.1, 0.200, 0.3, 0.212, "Moyenne")])
    assert [l.text for l in lignes] == ["MED355 Pharmacologie 14,25", "Moyenne"]
    assert isinstance(lignes[0], Ligne) and lignes[0].x0 == 0.1 and lignes[0].x1 == 0.6
    # rotation nulle : le point ne bouge pas ; 180° : symétrique par rapport au centre
    assert _unrotate(10, 20, 100, 200, 100, 200, 0) == (10, 20)
    x, y = _unrotate(10, 20, 100, 200, 100, 200, 180)
    assert round(x) == 90 and round(y) == 180


def test_pdf_falsifie_divergences_situees(svc, docs):
    rel = _n5(svc)
    ident = svc.verify_identify(str(docs["pdf"]))
    assert ident["matricule"] == "18M042" and ident["best"] == rel["id"] and not ident["delivered"]
    assert ident["soumis"] == {"nature": "PDF texte", "pages": 1, "ocr": None, "positions": True}
    cmp = svc.verify_compare(str(docs["pdf"]), rel["id"], demandeur="Ambassade (fictif)", reference="2026/0412")
    bad = [r for r in cmp["rows"] if not r["conforme"]]
    assert [(r["cle"], r["soumis"], r["original"]) for r in bad] == [("ue", "16,90", "14,90"), ("moyenne", "14,15", "13,15")]
    assert bad[0]["code"] == "MED502"
    # chaque champ est situé sur les deux documents : même ligne, même page
    for r in cmp["rows"]:
        assert r["pos"]["soumis"] and r["pos"]["original"], r["champ"]
    ue = bad[0]["pos"]
    assert ue["original"]["page"] == rel["numero_page"] and ue["soumis"]["page"] == 1
    assert abs(ue["original"]["y0"] - ue["soumis"]["y0"]) < 0.01 and 0 < ue["soumis"]["y0"] < ue["soumis"]["y1"] < 1
    moy = bad[1]["pos"]
    assert moy["soumis"]["y0"] > ue["soumis"]["y1"]           # la moyenne est sous le tableau des notes
    rep = svc.verify_report(str(docs["pdf"]), rel["id"], demandeur="Ambassade (fictif)", reference="2026/0412")
    assert Path(rep["path"]).exists()


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Tesseract absent")
def test_scan_boites_ocr_et_un_seul_ocr(svc, docs, monkeypatch):
    from core import verification
    rel = _n5(svc)
    calls = []
    real = verification._ocr_image
    monkeypatch.setattr(verification, "_ocr_image", lambda *a, **k: calls.append(1) or real(*a, **k))
    ident = svc.verify_identify(str(docs["png"]))
    assert ident["matricule"] == "18M042" and ident["soumis"]["nature"] == "Image" and ident["soumis"]["ocr"] > 50
    cmp = svc.verify_compare(str(docs["png"]), rel["id"])
    assert len(calls) == 1                                    # identification puis comparaison : un seul OCR
    ue = next(r for r in cmp["rows"] if r["code"] == "MED502")
    assert not ue["conforme"] and ue["soumis"] == "16,90"
    box = ue["pos"]["soumis"]
    assert box and abs(box["y0"] - ue["pos"]["original"]["y0"]) < 0.03   # scan de travers : à peu près la même ligne
    scan = svc.verify_compare(str(docs["scan"]), rel["id"])
    assert scan["soumis"]["nature"] == "PDF scanné" and next(r for r in scan["rows"] if r["code"] == "MED502")["pos"]["soumis"]
