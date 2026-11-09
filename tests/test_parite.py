"""Version 2 : fonctions de la 1.x reprises par le moteur (core/service.py), sur des archives fictives."""
from __future__ import annotations

import csv
import dataclasses
from pathlib import Path

import pytest

from core.service import Service, ServiceError


@pytest.fixture()
def svc(indexed, tmp_path, monkeypatch):
    monkeypatch.setenv("FMSP_DATA_DIR", str(tmp_path / "data"))      # réglages enregistrés à part
    _db, settings, _archives, _ = indexed
    s = dataclasses.replace(settings, exports_root=str(tmp_path / "Exports"), text_rules=list(settings.text_rules),
                            exclusions=list(settings.exclusions))
    return Service(s, previews=None)


def _pdf_releve(svc, q="18M042"):
    return next(r for r in svc.search(q)["results"] if not r["is_word"])


def _journal(svc, action):
    return [r for r in svc.journal(limit=5000) if r["action"] == action]


def test_consultation_journalisee_une_fois_par_releve(svc):
    rel = _pdf_releve(svc)
    before = len(_journal(svc, "CONSULTATION"))
    svc.releve(rel["id"])                         # sans « consult » (autre écran) : rien
    assert len(_journal(svc, "CONSULTATION")) == before
    svc.releve(rel["id"], consult=True)
    svc.releve(rel["id"], consult=True)           # ↓ ↑ sur le même relevé : une seule ligne
    rows = _journal(svc, "CONSULTATION")
    assert len(rows) == before + 1 and rows[0]["releve_id"] == rel["id"] and rel["periode"] in rows[0]["detail"]


def test_recherche_signale_le_matricule_normalise(svc):
    assert svc.search("18 m 042")["norm"] == "18M042"
    assert svc.search("18M042")["norm"] == ""          # déjà sous sa forme normale : rien à signaler
    assert svc.search("tchoua")["norm"] == ""


def test_controle_visuel_refuse_trace(svc):
    rel = _pdf_releve(svc)
    svc.visual_refused(rel["id"], "Page 3 illisible")
    assert _journal(svc, "IMPRESSION BLOQUÉE")[0]["detail"] == "Page 3 illisible"


def test_demande_cursus_valide_et_timbre_qui_refait_le_dossier(svc):
    rel = _pdf_releve(svc)
    did = svc.demande_create(demandeur="Lui-même")["demande"]["id"]
    with pytest.raises(ServiceError, match="Ajoutez d'abord"):
        svc.demande_add_cursus(did)
    svc.demande_add(did, rel["id"])
    res = svc.demande_add_cursus(did)
    expected = {r["id"] for r in svc.cursus_info("18M042")["releves"] if r["selected"]}
    ids = {it["item"]["releve_id"] for it in res["items"]}
    assert expected <= ids and res["added_count"] == len(expected - {rel["id"]})
    assert svc.demande_add_cursus(did)["added_count"] == 0          # rien en double
    # dossier prêt, puis le timbre Duplicata change : le dossier est à refaire (sinon imprimé sans timbre)
    svc.db.update_demande(did, statut="PRÊTE")
    assert svc.demande_update(did, {"duplicata": 1})["demande"]["statut"] == "EN COURS"
    svc.db.update_demande(did, statut="PRÊTE")
    assert svc.demande_update(did, {"motif": "Concours"})["demande"]["statut"] == "PRÊTE"


def test_integrite_liens_orphelins(svc, indexed, tmp_path):
    out = svc.check_integrity()
    assert out["orphelins"] == 0
    db = svc.db
    fid = db.conn.execute("SELECT id, chemin FROM fichiers LIMIT 1").fetchone()
    db.conn.execute("UPDATE fichiers SET chemin=? WHERE id=?", (str(tmp_path / "deplace.pdf"), fid["id"]))
    db.conn.commit()
    try:
        out = svc.check_integrity()
        assert out["orphelins"] == 1
        a = svc.anomalies("AVERTISSEMENT")[0]
        assert a["action"] == "Relocaliser" and "orphelin" in a["message"]
    finally:
        db.conn.execute("UPDATE fichiers SET chemin=? WHERE id=?", (fid["chemin"], fid["id"]))
        db.conn.commit()


def test_journal_exporte_en_csv_et_chemin_propose(svc, tmp_path):
    rel = _pdf_releve(svc)
    svc.releve(rel["id"], consult=True)
    path = svc.suggest_path("journal", "Journal.csv")
    assert "Journal" in path and path.endswith(".csv") and str(tmp_path / "Exports") in path
    assert "Extractions" in svc.suggest_path("extraction", "Decanat.xlsx")
    with pytest.raises(ServiceError):
        svc.suggest_path("../../etc", "x.csv")
    out = svc.journal_export(path, matricule="18M042")
    with open(out["path"], encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    assert rows[0][:4] == ["Horodatage", "Opérateur", "Poste", "Action"]
    assert out["count"] == len(rows) - 1 > 0 and all(r[4] == "18M042" for r in rows[1:])
    assert Path(svc.exports_dir()).is_dir()


def test_verification_journalisee_et_original_imprime_avec_timbre(svc):
    rel = _pdf_releve(svc)
    exp = svc.export(rel["id"])
    cmp = svc.verify_compare(exp["path"], rel["id"], demandeur="Ambassade", reference="12/AB")
    assert cmp["conformes"] == cmp["total"]
    row = _journal(svc, "VERIFICATION")[0]
    assert "Ambassade" in row["detail"] and "12/AB" in row["detail"] and "0 divergence" in row["detail"]
    prep = svc.verify_print_prepare(rel["id"])
    assert Path(prep["file"]).exists()
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(prep["file"])
    text = doc[0].get_textpage().get_text_range()
    doc.close()
    assert "VÉRIFIÉ" in text or "VERIFIE" in text.upper()
    svc.verify_printed(rel["id"], how="test", file=prep["file"])
    assert not Path(prep["file"]).exists()
    assert _journal(svc, "IMPRESSION")[0]["detail"].startswith("Original vérifié")


def test_reglages_chemins_et_options_de_la_1x(svc):
    s = svc.settings_get()
    for k in ("ocr_preprocess", "word_com_enabled", "tesseract_cmd", "soffice_cmd", "db_path", "cache_dir"):
        assert k in s
    with pytest.raises(ServiceError, match="disque local"):
        svc.settings_save({"db_path": r"\\SRV-FMSP\partage\releves.db"})
    before = svc.db_path
    out = svc.settings_save({"tesseract_cmd": "  C:/OCR/tesseract.exe ", "ocr_preprocess": False,
                             "db_path": str(Path(before).with_name("autre.db"))})
    assert out["tesseract_cmd"] == "C:/OCR/tesseract.exe" and out["ocr_preprocess"] is False
    assert svc.db_path == before          # nouvelle base : au redémarrage seulement
    info = svc.info()
    assert info["db_path"] == str(before) and info["config_path"].endswith("config.json")
    assert info["default_workers"] >= 1 and "multi_pages" in info["stats"]


def test_arret_force_apres_delai_et_cause_d_un_arret_anormal(tmp_path, monkeypatch):
    import subprocess
    import sys
    import time
    from engine import server
    monkeypatch.setattr(server, "_kill_orphan_words", lambda: 0)
    eng = object.__new__(server.Engine)
    eng._stopping, eng._run_gen = False, 1
    eng.FORCE_STOP_AFTER = 0.3
    # robot qui ignore « stop » : arrêté de force au bout du délai
    eng.indexer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.PIPE)
    assert eng.index_stop()["stopping"] and eng._stopping
    t = time.time()
    while eng.indexer.poll() is None and time.time() - t < 10:
        time.sleep(0.05)
    assert eng.indexer.poll() is not None
    # un minuteur d'une indexation précédente ne touche pas à la suivante
    eng.indexer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.PIPE)
    old = eng._run_gen
    eng._run_gen += 1
    eng._force_stop(eng.indexer, old)
    assert eng.indexer.poll() is None
    eng.indexer.kill()
    log = tmp_path / "indexation.log"
    log.write_text("===== 2026-11-09 robot\nTraceback (most recent call last):\n  File \"x.py\"\n"
                   "PermissionError: [Errno 13] accès refusé : releves.db\n", encoding="utf-8")
    assert server._last_error_line(log).startswith("PermissionError")
