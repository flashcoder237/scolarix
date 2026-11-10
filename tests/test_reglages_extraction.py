"""Réglages (outils du poste, diagnostic, aperçu AVANT / APRÈS des remplacements) et Extraction / Journal (2.1).

Données fictives uniquement (tools/generate_samples.py)."""
from __future__ import annotations

import csv
import time
from pathlib import Path

import pytest

from conftest import HAS_CONVERTER
from core.config import DEFAULT_TEXT_RULES
from core.service import Service, ServiceError

RULES = [dict(r) for r in DEFAULT_TEXT_RULES]
SAMPLE = "Tel  : 33 01 41 39  / 33 01 41 29.  RELEVE DE NOTES /TRANSCRIPT   FMSP/VDAAP/VDSSSE/SSSE"


@pytest.fixture()
def svc(indexed, tmp_path):
    db, settings, _archives, _ = indexed
    settings.exports_root = str(tmp_path / "exports")
    return Service(settings, db_factory=lambda: db)


# ------------------------------------------------------------------ extraction : parité, effectifs, export
@pytest.mark.parametrize("fil", [None, "M", "P"])
@pytest.mark.parametrize("last", [True, False])
@pytest.mark.parametrize("statut", ["tous", "admis", "non_valides"])
def test_extraction_view_meme_resultat_que_l_extraction_1x(svc, fil, last, statut):
    """Toutes les lignes, sans plafond, et exactement celles de Database.extraction (export identique)."""
    view = svc.extraction_view(filiere=fil, statut=statut, dernier_niveau=last)
    old = svc.db.extraction(fil, None, None, statut, last)
    assert view["count"] == len(old) == len(view["rows"])
    ids = [r[view["cols"].index("id")] for r in view["rows"]]
    assert ids == [r.id for r in old]


def test_extraction_view_effectifs_des_filtres(svc):
    v = svc.extraction_view(dernier_niveau=True)
    # l'effectif d'une filière est le nombre de lignes obtenues en la choisissant
    for code, n in v["filieres"].items():
        if code != "?":
            assert svc.extraction_view(filiere=code, dernier_niveau=True)["count"] == n
    assert v["filieres_total"] == v["count"]
    assert v["statuts"]["admis"] + v["statuts"]["non_valides"] == v["statuts"]["tous"] == v["count"]
    assert sum(v["mentions"].values()) == v["count"]
    assert sum(v["niveaux"].values()) == v["count"]
    # mentions et niveaux choisis : filtres appliqués, effectifs cohérents
    m = max(v["mentions"], key=v["mentions"].get)
    assert svc.extraction_view(dernier_niveau=True, mentions=[m])["count"] == v["mentions"][m]
    n = max(v["niveaux"], key=v["niveaux"].get)
    assert svc.extraction_view(dernier_niveau=True, niveaux=[n])["count"] == v["niveaux"][n]
    # années : l'histogramme ignore le filtre des années, le résultat le respecte
    y = int(v["per_year"][-1][0][:4])
    w = svc.extraction_view(dernier_niveau=False, year_from=y, year_to=y)
    assert {a for a, _ in w["per_year"]} == {a for a, _ in svc.extraction_view(dernier_niveau=False)["per_year"]}
    assert all(r[w["cols"].index("annee")].startswith(str(y)) for r in w["rows"])


def test_extraction_view_colonnes_supplementaires(svc):
    v = svc.extraction_view(extra_cols=["decision", "credits", "inconnue"])
    assert v["cols"][-2:] == ["decision", "credits"] and len(v["rows"][0]) == len(v["cols"])


def test_extraction_view_export_journalise_et_derniere_extraction(svc, tmp_path):
    v = svc.extraction_view(dernier_niveau=True, statut="admis")
    dest = tmp_path / "x.csv"
    r = svc.extraction_view_export("csv", str(dest), "Toutes · début → aujourd'hui", statut="admis", dernier_niveau=True)
    assert r["count"] == v["count"]
    with open(dest, encoding="utf-8-sig") as fh:
        assert sum(1 for _ in csv.reader(fh, delimiter=";")) == v["count"] + 1
    recent = svc.extraction_recent()
    assert recent[0]["lignes"] == v["count"] and recent[0]["format"] == "CSV" and recent[0]["existe"]
    assert recent[0]["scope"] == "Toutes · début → aujourd'hui"
    with pytest.raises(ServiceError):
        svc.extraction_view_export("csv", str(tmp_path / "vide.csv"), "x", mentions=[])


def test_extraction_view_50000_lignes(svc, monkeypatch):
    """Performance : 50 000 relevés (copies fictives des relevés indexés), toutes les lignes renvoyées."""
    import dataclasses
    base = svc.db.extraction(None, None, None, "tous", False)
    big = []
    for k in range(50000):
        r = base[k % len(base)]
        big.append(dataclasses.replace(r, id=100000 + k, matricule=f"{r.matricule[:2]}X{k:05d}"))
    monkeypatch.setattr(svc.db, "extraction", lambda *a, **kw: big)
    t0 = time.perf_counter()
    v = svc.extraction_view(dernier_niveau=True)
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    v2 = svc.extraction_view(dernier_niveau=False, filiere="M", statut="admis")
    again = time.perf_counter() - t0
    assert v["count"] == 50000 and len(v["rows"]) == 50000 and v2["count"] > 0
    assert first < 6 and again < 3, (first, again)


# ------------------------------------------------------------------ journal : pages, filtres, effectifs
def test_journal_view_pages_filtres_export(svc, tmp_path):
    for i in range(30):
        svc.db.log("CONSULTATION" if i % 3 else "IMPRESSION", "Agent Fictif", "G9", None, f"99M{i:03d}", f"essai {i}")
    page = svc.journal_view(period="", limit=10)
    assert page["total"] >= 30 and len(page["rows"]) == 10
    nxt = svc.journal_view(period="", offset=10, limit=10, facets=False)
    assert {r["id"] for r in nxt["rows"]}.isdisjoint({r["id"] for r in page["rows"]}) and "actions" not in nxt
    counts = dict(page["actions"])
    only = svc.journal_view(period="", actions=["IMPRESSION"])
    assert only["total"] == counts["IMPRESSION"] and all(r["action"] == "IMPRESSION" for r in only["rows"])
    assert svc.journal_view(period="", actions=[])["total"] == 0
    sans = svc.journal_view(period="", sans_actions=["IMPRESSION"])
    assert sans["total"] == page["total"] - counts["IMPRESSION"]
    assert dict(sans["actions"])["IMPRESSION"] == counts["IMPRESSION"]      # l'effectif reste affiché
    assert svc.journal_view(period="", operateur="Agent Fictif · G9")["total"] == 30
    assert svc.journal_view(period="", q="99M007")["total"] == 1
    assert page["periodes"]["0"] >= 30 and page["periodes"][""] >= page["periodes"]["7"]
    r = svc.journal_view_export(str(tmp_path / "j.csv"), period="", operateur="Agent Fictif · G9")
    assert r["count"] == 30


# ------------------------------------------------------------------ outils du poste, diagnostic, cache
def test_tools_status_et_diagnostic(svc, tmp_path):
    t = svc.tools_status(tesseract_cmd=str(tmp_path / "absent.exe"))
    assert t["tesseract"]["found"] is False and "introuvable" in t["tesseract"]["detail"]
    assert set(t["counts"]) >= {"scans_en_attente", "scans", "doc", "docx", "anomalies"}
    assert "found" in t["libreoffice"] and "platform" in t["word"]
    d = svc.diagnostic()
    assert d["version"] and d["releves"] > 0 and Path(d["config_path"]).name == "config.json"


def test_cache_clear_demande_confirmation(svc):
    with pytest.raises(ServiceError):
        svc.cache_clear()


# ------------------------------------------------------------------ aperçu AVANT / APRÈS des règles
def test_rules_preview_texte_libre(svc):
    r = svc.rules_preview(RULES, text=SAMPLE)
    assert r["mode"] == "texte" and r["count"] == 3
    assert r["text"] == svc.rules_test(RULES, SAMPLE)["text"]
    dels = "".join(s["t"] for p in r["avant"] for s in p["seg"] if s["k"] == "del")
    ins = [s for p in r["apres"] for s in p["seg"] if s["k"] == "ins"]
    assert "Tel" in dels and "33 01 41 29." in dels and "FMSP/VDAAP" in dels
    assert any(s["t"] == "Ref N°" and s.get("size") == 8 for s in ins)        # mise en forme de la règle
    with pytest.raises(ServiceError, match="Règle 1"):
        svc.rules_preview([{"actif": True, "regex": True, "chercher": "(x", "remplacer": ""}], text="x")


def _word_releve(svc):
    rows = svc.rules_preview_releves()
    assert rows and all(r["label"].endswith((".doc)", ".docx)")) for r in rows)
    return next(r for r in rows if r["label"].endswith(".docx)"))


TITLE_RULE = {"actif": True, "description": "Titre", "chercher": "RELEVÉ DE NOTES", "remplacer": "Ref N° RELEVÉ DE NOTES",
              "taille": 7, "gras": False}


def test_rules_preview_entete_typographique(svc):
    rel = _word_releve(svc)
    r = svc.rules_preview(RULES + [TITLE_RULE], releve_id=rel["id"])
    assert r["mode"] == "releve" and r["count"] >= 1
    texte = ["".join(s["t"] for s in p["seg"]) for p in r["apres"]]
    assert any("Ref N° RELEVÉ DE NOTES" in t for t in texte)
    ins = [s for p in r["apres"] for s in p["seg"] if s["k"] == "ins"]
    assert any("Ref" in s["t"] and s.get("size") == 7 for s in ins)
    assert rel["id"] and svc.rules_preview_releves(q=rel["label"].split(" ")[0])
    with pytest.raises(ServiceError):                    # un PDF n'a pas d'en-tête Word à corriger
        pdf = svc.db.conn.execute("SELECT r.id FROM etudiants_releves r JOIN fichiers f ON f.id = r.fichier_id "
                                  "WHERE f.type_fichier = 'PDF_TEXT' LIMIT 1").fetchone()[0]
        svc.rules_preview(RULES, releve_id=pdf)


@pytest.mark.skipif(not HAS_CONVERTER, reason="LibreOffice / Word absent")
def test_rules_preview_rendu_du_moteur(indexed, tmp_path):
    from core.preview import PreviewService
    db, settings, _a, _ = indexed
    pv = PreviewService(db, settings)
    try:
        svc = Service(settings, pv, db_factory=lambda: db)
        rel = _word_releve(svc)
        r = svc.rules_preview(RULES + [TITLE_RULE], releve_id=rel["id"], rendu=True, req=1)
        assert r["count"] >= 1 and r["rendu"]["avant"]["img"].startswith("data:image/png;base64,")
        assert any(x["k"] == "ins" for x in r["rendu"]["apres"]["rects"])      # « Ref N° » situé sur l'image
        assert r["motif"] is None                                             # l'aperçu réel garderait la correction
        assert svc.rules_preview(RULES, releve_id=rel["id"], rendu=True, req=0).get("stale")   # rendu dépassé
    finally:
        pv.close()
