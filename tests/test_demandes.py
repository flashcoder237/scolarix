"""Demandes : réunir plusieurs relevés d'une personne, préparer le dossier, suivre son statut."""
from __future__ import annotations

from pypdf import PdfReader

from core import demandes, pdf_tools
from core.preview import PreviewService


def test_demande_complete(indexed, tmp_path):
    db, settings = indexed[0], indexed[1]
    settings.exports_root = str(tmp_path / "Exports")
    rels = pdf_tools.select_for_cursus(db.cursus("18M042"))[:3]
    did = db.create_demande(settings.poste, settings.operateur, rels[0].matricule, rels[0].nom_complet,
                            "L'étudiant lui-même", "Inscription en master")
    d = db.demande(did)
    assert d["statut"] == "EN COURS" and d["numero"].count("-") == 2
    for r in rels:
        assert db.add_demande_item(did, r.id)
    assert not db.add_demande_item(did, rels[0].id)          # pas de doublon
    db.reorder_demande(did, [r.id for r in reversed(rels)])
    svc = PreviewService(db, settings)
    prep = demandes.prepare(db, settings, did, svc)
    svc.close()
    assert len(prep.prets) == 3 and not prep.bloques
    files = sorted(p.name for p in prep.dossier.glob("*.pdf"))
    assert files[0] == demandes.BORDEREAU and demandes.A_IMPRIMER in files
    assert [f[:3] for f in files if f[0].isdigit() and f != demandes.BORDEREAU] == ["01_", "02_", "03_"]
    assert prep.dossier.parent.parent.parent.name == "Demandes" and d["numero"] in prep.dossier.name
    merged = PdfReader(str(prep.a_imprimer))
    assert len(merged.pages) == prep.pages
    # ordre respecté : le 1er fichier est le dernier relevé ajouté (ordre inversé)
    assert rels[-1].matricule in merged.pages[0].extract_text()
    bord = PdfReader(str(prep.bordereau)).pages[0].extract_text()
    assert d["numero"] in bord and "Inscription en master" in bord and prep.prets[0][1] in bord
    assert db.demande(did)["statut"] == "PRÊTE"
    items = db.demande_items(did)
    assert all(i["etat"] == "PRÊT" and i["reference"] for i, _ in items)
    demandes.mark(db, settings, did, "IMPRIMÉE")
    demandes.mark(db, settings, did, "REMISE", "remis en main propre")
    assert db.demande(did)["statut"] == "REMISE"
    assert db.demandes(demandes.OUVERTES) == [] or all(r["id"] != did for r in db.demandes(demandes.OUVERTES))
    # modifier une demande la renvoie « EN COURS » (dossier à refaire)
    db.remove_demande_item(did, rels[0].id)
    assert db.demande(did)["statut"] == "EN COURS" and db.demande(did)["n"] == 2


def test_releve_bloque_ecarte_et_signale(indexed, tmp_path):
    db, settings = indexed[0], indexed[1]
    settings.exports_root = str(tmp_path / "Exports")
    good = next(r for r in db.search("18M042") if r.type_fichier.startswith("PDF"))
    did = db.create_demande(settings.poste, settings.operateur, good.matricule, good.nom_complet)
    db.add_demande_item(did, good.id)
    db.add_demande_item(did, 999999)                          # relevé disparu de l'index
    prep = demandes.prepare(db, settings, did)
    assert len(prep.prets) == 1 and len(prep.bloques) == 1
    assert "Non joints" in PdfReader(str(prep.bordereau)).pages[0].extract_text()
