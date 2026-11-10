"""Revue de la demande, page Demandes et aperçu du cursus fusionné (moteur), sur des archives fictives."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from pypdf import PdfReader

from core import demandes
from core.service import Service, ServiceError


@pytest.fixture()
def svc(indexed, tmp_path, monkeypatch):
    monkeypatch.setenv("FMSP_DATA_DIR", str(tmp_path / "data"))
    _db, settings, _archives, _ = indexed
    s = dataclasses.replace(settings, exports_root=str(tmp_path / "Exports"), text_rules=list(settings.text_rules),
                            exclusions=list(settings.exclusions))
    return Service(s, previews=None)


def _pdf_releves(svc, n=2):
    rels = [r for r in svc.cursus_info("18M042")["releves"] if not r["is_word"] and r["selected"]]
    assert len(rels) >= n
    return rels[:n]


def _journal(svc, action):
    return [r for r in svc.journal(limit=5000) if r["action"] == action]


def _deliveries(svc):
    return svc.db.conn.execute("SELECT COUNT(*) FROM delivrances").fetchone()[0]


def test_controle_a_l_ecran_trace_et_etapes_horodatees(svc):
    a, b = _pdf_releves(svc)
    did = svc.demande_create(demandeur="Lui-même")["demande"]["id"]
    svc.demande_add(did, a["id"])
    svc.demande_add(did, b["id"])
    rv = svc.demande_review(did)
    assert rv["review"]["controles"] == [] and rv["review"]["etapes"]["preparer"] is None
    assert rv["review"]["bordereau"] is None and not any(it["prepared"] for it in rv["items"])
    rv = svc.demande_control(did, a["id"], True)
    assert rv["review"]["controles"] == [a["id"]]
    numero = rv["demande"]["numero"]
    row = _journal(svc, "CONTRÔLE VISUEL")[0]
    assert row["releve_id"] == a["id"] and row["detail"].startswith(f"Demande {numero} ·")
    rv = svc.demande_control(did, a["id"], False)
    assert rv["review"]["controles"] == [] and _journal(svc, "CONTRÔLE VISUEL RETIRÉ")
    svc.demande_control_skipped(did, [b["id"]])
    assert _journal(svc, "CONTRÔLE VISUEL OMIS")[0]["releve_id"] == b["id"]
    # préparation : fichiers préparés montrés, heure de l'étape, bordereau réel
    svc.demande_prepare(did)
    rv = svc.demande_review(did)
    assert rv["demande"]["statut"] == "PRÊTE" and rv["review"]["etapes"]["preparer"]
    assert all(it["prepared"] for it in rv["items"]) and Path(rv["review"]["bordereau"]).exists()
    assert Path(rv["review"]["a_imprimer"]).exists()
    svc.demande_mark(did, "IMPRIMÉE", "test")
    assert svc.demande_review(did)["review"]["etapes"]["imprimer"]
    # retirer un relevé : la demande repart EN COURS, l'heure de préparation ne vaut plus
    svc.demande_remove(did, b["id"])
    rv = svc.demande_review(did)
    assert rv["review"]["etapes"]["preparer"] is None and not any(it["prepared"] for it in rv["items"])


def test_duplicata_sur_demande_imprimee_refait_le_dossier(svc):
    (a,) = _pdf_releves(svc, 1)
    did = svc.demande_create(demandeur="Lui-même")["demande"]["id"]
    svc.demande_add(did, a["id"])
    svc.db.update_demande(did, statut="IMPRIMÉE")
    assert svc.demande_update(did, {"duplicata": 1})["demande"]["statut"] == "EN COURS"


def test_bordereau_provisoire_sans_numero(svc):
    a, b = _pdf_releves(svc)
    did = svc.demande_create(demandeur="Sa mère", motif="Concours")["demande"]["id"]
    svc.demande_add(did, a["id"])
    svc.demande_add(did, b["id"])
    before = _deliveries(svc)
    out = svc.demande_bordereau_preview(did)
    text = PdfReader(out["path"]).pages[0].extract_text()
    assert "PROVISOIRE" in text and "Concours" in text and "à attribuer" in text
    assert _deliveries(svc) == before
    assert svc.demande_bordereau_preview(did)["path"] == out["path"]         # inchangée : pas refait
    svc.demande_update(did, {"motif": "Bourse"})
    again = svc.demande_bordereau_preview(did)
    assert again["path"] != out["path"] and not Path(out["path"]).exists()


def test_tableau_de_bord_des_demandes(svc):
    a, b = _pdf_releves(svc)
    did = svc.demande_create(demandeur="Lui-même")["demande"]["id"]
    svc.demande_add(did, a["id"])
    svc.demande_add(did, b["id"])
    row = next(r for r in svc.demandes_board(["EN COURS"]) if r["id"] == did)
    assert row["n"] == 2 and row["pages"] == a["nb_pages"] + b["nb_pages"] and row["bloques"] == 0


def test_apercu_du_cursus_sans_numero_ni_journal(svc):
    svc.s.delivery_ref_stamp = True              # pied de page de délivrance : présent dans l'aperçu, sans numéro
    info = svc.cursus_info("18M042")
    ids = [r["id"] for r in info["releves"] if not r["is_word"] and r["selected"]]
    before, journal = _deliveries(svc), len(svc.journal(limit=5000))
    out = svc.cursus_build("18M042", ids, cover=True, duplicata=False, mode="preview")
    assert out["ref"] is None and out["preview"] and Path(out["path"]).exists()
    assert _deliveries(svc) == before and len(svc.journal(limit=5000)) == journal
    reader = PdfReader(out["path"])
    assert len(reader.pages) == out["pages"]
    cover = out["layout"][0]
    assert cover["releve_id"] is None and cover["first"] == 1
    assert [x["releve_id"] for x in out["layout"][1:]] == ids
    assert out["layout"][-1]["last"] == out["pages"]
    assert "attribuée à l'impression" in reader.pages[-1].extract_text()
    again = svc.cursus_build("18M042", ids, cover=True, duplicata=False, mode="preview")
    assert again["cached"] and again["path"] == out["path"]
    no_cover = svc.cursus_build("18M042", ids, cover=False, duplicata=False, mode="preview")
    assert no_cover["pages"] == out["pages"] - cover["last"]
    with pytest.raises(ServiceError):
        svc.cursus_build("18M042", [], mode="preview")
    # l'export garde son numéro de délivrance et sa ligne au journal
    exp = svc.cursus_build("18M042", ids, cover=True, mode="export")
    assert exp["ref"] and _deliveries(svc) == before + 1


def test_exemplaire_choisi_garde_a_la_fusion(svc, monkeypatch):
    a, b = _pdf_releves(svc)
    # a et b présentés comme deux exemplaires du même relevé : seul le premier coché est fusionné
    rels = [svc.db.get(a["id"]), svc.db.get(b["id"])]
    group = [a["id"], b["id"]]
    monkeypatch.setattr(svc.db, "_dup_groups", lambda mats: ({a["id"]: group, b["id"]: group}, {}))
    monkeypatch.setattr(svc.db, "get", lambda i: next(r for r in rels if r.id == int(i)))
    assert [r.id for r in svc._cursus_rels([b["id"], a["id"]])] == [b["id"]]
    assert [r.id for r in svc._cursus_rels([a["id"], b["id"]])] == [a["id"]]


def test_bordereau_provisoire_signale_un_releve_disparu(svc, tmp_path):
    (a,) = _pdf_releves(svc, 1)
    did = svc.demande_create(demandeur="Lui-même")["demande"]["id"]
    svc.demande_add(did, a["id"])
    svc.db.add_demande_item(did, 999999)
    dest = demandes.bordereau_provisoire(svc.db, svc.s, did, tmp_path / "b.pdf")
    assert "Non joints" in PdfReader(str(dest)).pages[0].extract_text()
