"""Robot d'indexation : parallélisme, exclusions, différentiel, arrêt, ligne de commande."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import HAS_CONVERTER, ROOT, make_settings

from core.database import Database
from core.indexer import (Exclusions, FileProcessor, Indexer, SourceFile, default_workers, scan_sources)


def releves_of(db):
    return sorted((Path(r.chemin).name, r.matricule, r.numero_page, r.page_fin, r.statut or "")
                  for r in db._select("1=1", []))


def test_parallele_identique_au_sequentiel(tmp_path, demo_archives, data_dir):
    out = {}
    for workers in (1, 3):
        s = make_settings(tmp_path / f"w{workers}", ocr_enabled=False)
        db = Database(s.db_path, cache_dir=s.resolved_cache_dir())
        c = Indexer(s, db).run(demo_archives, only_new=False, workers=workers)
        out[workers] = (releves_of(db), c.echecs, c.ecrits)
    assert out[1] == out[3]
    assert out[1][2] >= 50


def test_evenements_de_progression(tmp_path, demo_archives, data_dir):
    events = []
    s = make_settings(tmp_path, ocr_enabled=False)
    db = Database(s.db_path)
    Indexer(s, db, events.append).run(demo_archives, only_new=False, workers=2)
    types = [e["type"] for e in events]
    assert types[0] == "scan" and types[1] == "start" and types[-1] == "done"
    start = events[1]
    assert start["workers"] == 2 and start["a_traiter"] == start["total"]
    last = [e for e in events if e["type"] == "progress"][-1]
    assert last["done"] == last["total"]
    assert events[-1]["duree_s"] >= 0 and events[-1]["stopped"] is False


def test_exclusions_dossier_et_motif(tmp_path, demo_archives, data_dir):
    sub = demo_archives / "DIPLOMES 2022"
    sub.mkdir()
    some_pdf = next(demo_archives.rglob("*.pdf"))
    shutil.copy(some_pdf, sub / "copie.pdf")
    shutil.copy(some_pdf, demo_archives / "DIPLOME SP 22-23.pdf")
    deep = demo_archives / "Brouillons" / "vieux"
    deep.mkdir(parents=True)
    shutil.copy(some_pdf, deep / "brouillon.pdf")

    all_files = {f.path for f in scan_sources(demo_archives)}
    excl = Exclusions(demo_archives, ["*diplome*", "Brouillons"])   # casse et accents ignorés
    kept = {f.path for f in scan_sources(demo_archives, excl)}
    assert not any("DIPLOME" in p.upper() or "Brouillons" in p for p in kept)
    assert len(all_files) - len(kept) == 3
    # chemin absolu d'un sous-dossier
    excl2 = Exclusions(demo_archives, [str(deep.parent)])
    assert not any("Brouillons" in f.path for f in scan_sources(demo_archives, excl2))
    # chemin relatif à la racine
    excl3 = Exclusions(demo_archives, [os.path.join("Brouillons", "vieux")])
    assert not any("vieux" in f.path for f in scan_sources(demo_archives, excl3))


def test_exclusion_retire_de_l_index_sans_toucher_au_disque(tmp_path, demo_archives, data_dir):
    s = make_settings(tmp_path, ocr_enabled=False)
    db = Database(s.db_path)
    Indexer(s, db).run(demo_archives, only_new=False, workers=1)
    target = next(p for p in demo_archives.iterdir() if p.is_dir())
    n_before = len([f for f in db.file_signatures() if f.startswith(str(target.resolve()))])
    assert n_before > 0
    s.exclusions = [target.name]
    events = []
    c = Indexer(s, db, events.append).run(demo_archives, only_new=True, workers=1)
    assert next(e for e in events if e["type"] == "start")["retires"] == n_before
    assert not [f for f in db.file_signatures() if f.startswith(str(target.resolve()))]
    assert target.exists() and any(target.rglob("*"))          # rien n'est supprimé sur le disque
    assert c.exclus >= 1


def test_ignore_les_fichiers_temporaires_office(tmp_path, demo_archives):
    (demo_archives / "~$EM2_S2.docx").write_bytes(b"lock")
    (demo_archives / "Thumbs.db").write_bytes(b"x")
    assert not any(Path(f.path).name.startswith("~$") for f in scan_sources(demo_archives))


def test_differentiel_ne_relit_que_les_fichiers_modifies(tmp_path, demo_archives, data_dir):
    s = make_settings(tmp_path, ocr_enabled=False)
    db = Database(s.db_path)
    Indexer(s, db).run(demo_archives, only_new=False, workers=2)
    c = Indexer(s, db).run(demo_archives, only_new=True, workers=2)
    assert c.extraits == 0 and c.ignores == c.detectes
    f = next(demo_archives.rglob("*.pdf"))
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 100))
    c = Indexer(s, db).run(demo_archives, only_new=True, workers=2)
    assert c.extraits + c.echecs == 1


def test_arret_propre(tmp_path, demo_archives, data_dir):
    s = make_settings(tmp_path, ocr_enabled=False)
    db = Database(s.db_path)
    idx = Indexer(s, db)

    def emit(e):
        if e["type"] == "start":
            idx.stop()           # comme le « stop » envoyé par l'interface
    idx.emit = emit
    c = idx.run(demo_archives, only_new=False, workers=2)
    assert idx.stopped
    assert c.extraits + c.echecs == 0
    # la base reste cohérente : une relance différentielle termine le travail
    c2 = Indexer(s, db).run(demo_archives, only_new=True, workers=2)
    assert c2.ignores >= c.extraits + c.echecs and not Indexer(s, db).stopped


def test_fichier_illisible_ne_bloque_pas(tmp_path, data_dir):
    bad = tmp_path / "faux.pdf"
    bad.write_bytes(b"ceci n'est pas un pdf")
    res = FileProcessor(make_settings(tmp_path)).process(SourceFile(str(bad), bad.stat().st_size, bad.stat().st_mtime))
    assert res.statut == "ERREUR" and "endommagé" in res.message and res.anomalies[0][0] == "ERREUR"


@pytest.mark.skipif(not HAS_CONVERTER, reason="LibreOffice non installé")
def test_doc_97_2003_lu_via_docx_intermediaire(indexed):
    db, settings = indexed[0], indexed[1]
    docs = [r for r in db._select("f.type_fichier = 'DOC'", [])]
    assert docs, "le fonds de démonstration contient un .doc lisible"
    from core.preview import intermediate_docx
    assert intermediate_docx(docs[0].chemin, settings.resolved_cache_dir()).exists()
    assert docs[0].cache_pdf is None          # pas de PDF à l'indexation : produit à la consultation


def test_nombre_de_processus_automatique():
    assert 1 <= default_workers() <= 4


def test_ligne_de_commande_exclusion_et_processus(tmp_path, demo_archives):
    (demo_archives / "DIPLOMES").mkdir()
    shutil.copy(next(demo_archives.rglob("*.pdf")), demo_archives / "DIPLOMES" / "a_ignorer.pdf")
    env = dict(os.environ, FMSP_DATA_DIR=str(tmp_path / "d"), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(ROOT / "main.py"), "--index", "--json", "--no-ocr",
                           "--source", str(demo_archives), "--db", str(tmp_path / "cli.db"),
                           "--workers", "2", "--exclude", "DIPLOMES"],
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=600)
    assert proc.returncode == 0, proc.stderr
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    assert events[-1]["type"] == "done" and events[-1]["workers"] == 2
    assert "a_ignorer" not in proc.stdout


def test_ligne_de_commande_arret_par_entree_standard(tmp_path, demo_archives):
    env = dict(os.environ, FMSP_DATA_DIR=str(tmp_path / "d"), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--index", "--json", "--no-ocr",
                             "--stdin-control", "--source", str(demo_archives), "--db", str(tmp_path / "s.db"),
                             "--workers", "2"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    for line in proc.stdout:
        if '"type": "start"' in line:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
            break
    rest = proc.stdout.read()
    proc.wait(timeout=120)
    assert proc.returncode == 0
    done = json.loads([ln for ln in rest.splitlines() if '"type": "done"' in ln][-1])
    assert done["stopped"] is True
