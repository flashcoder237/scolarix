"""Écran Indexation (2.1) : causes d'anomalies, marque « vu », pagination, relecture, historique des passes.
Données entièrement fictives."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from conftest import make_settings
from core import anomalies as causes
from core.database import Database
from core.indexer import Indexer
from core.service import Service


@pytest.mark.parametrize("niveau, message, code", [
    ("AVERTISSEMENT", "PDF scanné non lu : OCR désactivé ou Tesseract introuvable.", "ocr_off"),
    ("AVERTISSEMENT", "Page 4 : aucun matricule détecté.", "matricule"),
    ("AVERTISSEMENT", "Page 2 : en-tête d'étudiant sans matricule lisible — relevé isolé, matricule à saisir.", "matricule"),
    ("AVERTISSEMENT", "18M042 (page 3) : décision du jury non détectée.", "decision"),
    ("AVERTISSEMENT", "18M042 (page 3) : deux relevés mélangés sur la page (décision non retenue) — vérifier.", "melange"),
    ("AVERTISSEMENT", "18M042 (page 1) : titre « RELEVÉ DE NOTES » absent (attestation ?) — décision non retenue.", "titre"),
    ("AVERTISSEMENT", "18M042 (page 1) : année académique incohérente (2004-2007).", "annee"),
    ("AVERTISSEMENT", "18M042 (page 1) : date de naissance illisible.", "naissance"),
    ("AVERTISSEMENT", "OCR : confiance faible (< 70 %) sur la/les page(s) 2. À vérifier manuellement.", "ocr_faible"),
    ("AVERTISSEMENT", "Lien orphelin : fichier déplacé, renommé ou supprimé.", "orphelin"),
    ("AVERTISSEMENT", "Aucun relevé détecté dans ce fichier.", "aucun_releve"),
    ("ERREUR", "Fichier protégé par mot de passe : ignoré, indexation poursuivie.", "mot_de_passe"),
    ("ERREUR", "PDF illisible ou corrompu (PdfiumError).", "endommage"),
    ("ERREUR", "Fichier Word 97-2003 (.doc) : Microsoft Word ou LibreOffice est nécessaire.", "word_absent"),
    ("ERREUR", "Erreur inattendue : ValueError: x", "robot"),
    ("ERREUR", "quelque chose d'imprévu", "erreur_autre"),
    ("INFO", "2 relevé(s) sur plusieurs pages jumelé(s) (ex. 18M042 : pages 1–2).", "multipages"),
])
def test_classement_des_messages(niveau, message, code):
    assert causes.classify(niveau, message) == code


def test_dossier_annee_page():
    p = "/srv/Archives/2004-2005/Pharmacie/Lot_PH2.pdf"
    assert causes.folder_of(p) == "/srv/Archives/2004-2005/Pharmacie"
    assert causes.year_of(p) == "2004-2005"
    assert causes.year_of(r"\\SRV\Archives\1999_00\Med\a.pdf") == "1999-2000"
    assert causes.year_of("/srv/x/a.pdf") is None
    assert causes.page_of("Page 4 : aucun matricule détecté.") == 4
    assert causes.page_of("18M042 (page 3) : nom non détecté.") == 3


def test_migration_base_existante(tmp_path):
    """Base d'une version antérieure (et de la 1.3.1) : colonnes ajoutées, lignes classées, rien de perdu."""
    path = tmp_path / "ancienne.db"
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE anomalies (id INTEGER PRIMARY KEY AUTOINCREMENT,
        horodatage TEXT DEFAULT (datetime('now','localtime')), niveau TEXT NOT NULL, chemin TEXT,
        message TEXT NOT NULL, action TEXT);""")
    c.executemany("INSERT INTO anomalies(niveau, chemin, message, action) VALUES (?,?,?,?)", [
        ("AVERTISSEMENT", "/fictif/2001-2002/Med/a.pdf", "Page 1 : aucun matricule détecté.", "Revoir la page"),
        ("ERREUR", "/fictif/2001-2002/Med/b.doc", "Fichier protégé par mot de passe : ignoré.", None)])
    c.commit()
    c.close()
    db = Database(path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(anomalies)")}
    assert {"code", "dossier", "annee"} <= cols
    assert db.anomalies_backfill() == 2
    rows = {r["chemin"]: r for r in db.conn.execute("SELECT * FROM anomalies")}
    assert rows["/fictif/2001-2002/Med/a.pdf"]["code"] == "matricule"
    assert rows["/fictif/2001-2002/Med/b.doc"]["annee"] == "2001-2002"
    # l'application 1.3.1 continue d'écrire sans code : classé au prochain passage
    db.conn.execute("INSERT INTO anomalies(niveau, chemin, message, action) VALUES ('INFO', '/f/c.pdf', "
                    "'2 relevé(s) sur plusieurs pages jumelé(s)', NULL)")
    db.conn.commit()
    assert db.anomalies_backfill() == 1
    db.close()
    Database(path).close()      # réouverture : migration idempotente


def test_pagination_vu_export_10000(tmp_path, data_dir):
    import anomalies_fictives
    s = make_settings(tmp_path, source_root="/archives_fictives")
    anomalies_fictives.generate(s.db_path, 10000)
    svc = Service(s)
    assert len(svc.anomalies()) == 10000                          # plus de plafond à 500
    assert len(svc.anomalies(limit=50, offset=9990)) == 10
    t = time.perf_counter()
    summ = svc.anomalies_summary()
    page = svc.anomalies_files({"cause": "ocr_off"}, 0, 200)
    assert time.perf_counter() - t < 2.0
    assert summ["totaux"]["tout"]["n"] == 10000
    ocr = next(c for c in summ["causes"] if c["code"] == "ocr_off")
    assert page["total"] == ocr["fichiers"] and len(page["rows"]) == 200
    assert sum(d["n"] for d in page["dossiers"]) == page["total"]
    assert not page["dossiers"][0]["label"].startswith("/")          # relatif au dossier racine
    last = svc.anomalies_files({"cause": "ocr_off"}, page["total"] - 5, 200)
    assert len(last["rows"]) == 5
    # marquer vus deux fichiers, puis toute la cause
    two = [r["chemin"] for r in page["rows"][:2]]
    assert svc.anomalies_mark_seen(two, {"cause": "ocr_off"})["fichiers"] == 2
    assert svc.anomalies_files({"cause": "ocr_off"})["total"] == ocr["fichiers"] - 2
    assert svc.anomalies_files({"cause": "ocr_off", "vue": "vues"})["total"] == 2
    svc.anomalies_mark_seen(None, {"cause": "ocr_off"})
    assert not any(c["code"] == "ocr_off" for c in svc.anomalies_summary()["causes"])
    out = svc.anomalies_export(str(tmp_path / "a.csv"), two, {"cause": "ocr_off", "vue": "tout"})
    text = (tmp_path / "a.csv").read_text(encoding="utf-8-sig")
    assert out["count"] >= 2 and text.startswith("Niveau;Cause;Fichier") and "oui" in text


def test_vu_garde_apres_relecture_et_relecture_ciblee(tmp_path, data_dir, demo_archives):
    s = make_settings(tmp_path, ocr_enabled=False, source_root=str(demo_archives))
    db = Database(s.db_path, cache_dir=s.resolved_cache_dir())
    Indexer(s, db).run(demo_archives, only_new=False, workers=1)
    svc = Service(s, db_factory=lambda: db)
    summ = svc.anomalies_summary()
    assert summ["causes"], "le fonds fictif produit des anomalies"
    first = svc.anomalies_files({"cause": summ["causes"][0]["code"]})["rows"][0]
    svc.anomalies_mark_seen([first["chemin"]], {"cause": summ["causes"][0]["code"]})
    paths = svc.anomalies_paths([first["chemin"]], {"cause": summ["causes"][0]["code"], "vue": "tout"})
    assert paths == [first["chemin"]]
    # relecture de ce seul fichier : historique « RELECTURE », marque « vu » conservée
    k = Indexer(s, db).run(Path("/nulle/part"), paths=paths + ["/introuvable/x.pdf"], workers=1)
    assert k.detectes == 1 and k.extraits + k.echecs == 1
    det = svc.anomalies_detail(first["chemin"])
    assert any(a["vu"] for a in det["anomalies"])
    passes = db.passes()
    assert passes[0]["mode"] == "RELECTURE" and passes[0]["fin"] and passes[0]["lus"] == 1
    assert passes[1]["mode"] == "COMPLETE" and passes[1]["fichiers"] > 1
    ov = svc.index_overview()
    assert ov["sante"]["ok"] + ov["sante"]["avertissement"] + ov["sante"]["echec"] == ov["fichiers"]
    assert ov["passes"][0]["mode"] == "RELECTURE" and ov["formats"]
    # exclure un dossier : retiré de l'index tout de suite, exclusion enregistrée
    folder = det["dossier"]
    impact = svc.index_exclude_folder(folder)
    assert impact["fichiers"] >= 1 and not impact["deja"]
    done = svc.index_exclude_folder(folder, apply=True)
    assert folder in done["exclusions"]
    assert svc.anomalies_detail(first["chemin"])["anomalies"] == []
    # intégrité : une seule ligne par fichier orphelin, même vérifiée deux fois
    svc.check_integrity()
    svc.check_integrity()
    assert svc.index_overview()["integrite"]["verifiee"]
