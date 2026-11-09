"""Moteur en processus séparé : protocole, services, robustesse (aucune interface)."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class Client:
    def __init__(self, env):
        self.proc = subprocess.Popen([sys.executable, "-m", "engine.server"], cwd=ROOT, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.responses: dict[int, dict] = {}
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.cv = threading.Condition()
        self.n = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: [None for _ in self.proc.stderr], daemon=True).start()
        ev = self.events.get(timeout=60)
        assert ev["event"] == "ready"

    def _read(self):
        for raw in self.proc.stdout:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] is not None:
                with self.cv:
                    self.responses[msg["id"]] = msg
                    self.cv.notify_all()
            else:
                self.events.put(msg)

    def send(self, method, **params) -> int:
        self.n += 1
        self.proc.stdin.write((json.dumps({"id": self.n, "method": method, "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        return self.n

    def wait(self, mid, timeout=120) -> dict:
        with self.cv:
            ok = self.cv.wait_for(lambda: mid in self.responses, timeout)
        assert ok, f"pas de réponse à {mid}"
        return self.responses.pop(mid)

    def call(self, method, timeout=120, **params):
        res = self.wait(self.send(method, **params), timeout)
        if "error" in res:
            raise RuntimeError(res["error"]["message"])
        return res["result"]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(30)
        except Exception:
            self.proc.kill()


@pytest.fixture(scope="module")
def engine(indexed, tmp_path_factory):
    from core.config import save_settings
    db, settings, archives, _ = indexed
    home = tmp_path_factory.mktemp("engine_data")
    old = os.environ.get("FMSP_DATA_DIR")
    os.environ["FMSP_DATA_DIR"] = str(home)
    try:
        settings.exports_root = str(home / "Exports")
        save_settings(settings)
    finally:
        os.environ["FMSP_DATA_DIR"] = old or ""
    env = dict(os.environ, FMSP_DATA_DIR=str(home), PYTHONIOENCODING="utf-8")
    c = Client(env)
    c.home = home
    yield c
    c.close()


def test_ping_info_recherche(engine):
    assert engine.call("ping") == "pong"
    info = engine.call("info")
    assert info["app"] == "Scolarix" and info["stats"]["releves"] > 0
    res = engine.call("search", q="18-m-042")
    assert res["results"] and all(r["matricule"] == "18M042" for r in res["results"])
    r0 = res["results"][0]
    for key in ("periode", "pages_label", "is_word", "sem_label", "flags", "preview_pdf"):
        assert key in r0
    assert engine.call("search", q="")["results"] == []


def test_releve_alertes_controle_export(engine):
    rel = next(r for r in engine.call("search", q="18M042")["results"] if not r["is_word"])
    det = engine.call("releve", releve_id=rel["id"])
    assert det["alerts"] and det["preview_pdf"] and Path(det["preview_pdf"]).exists()
    assert engine.call("check", releve_id=rel["id"])["ok"]
    out = engine.call("export", releve_id=rel["id"], duplicata=True)
    assert Path(out["path"]).exists() and "DUPLICATA" in out["name"] and out["ref"]


def test_impression_preparee_puis_journalisee(engine):
    rel = next(r for r in engine.call("search", q="18M042")["results"] if not r["is_word"])
    before = engine.call("stats")["imprimes"]
    prep = engine.call("print_prepare", releve_id=rel["id"])
    assert Path(prep["file"]).exists() and prep["ref"]
    engine.call("print_done", releve_id=rel["id"], ref=prep["ref"], duplicata=False, how="test", file=prep["file"])
    assert engine.call("stats")["imprimes"] == before + 1
    assert not Path(prep["file"]).exists()          # fichier temporaire supprimé


def test_erreur_expliquee_et_methode_inconnue(engine):
    res = engine.wait(engine.send("releve", releve_id=999999))
    assert res["error"]["kind"] == "user" and "n'existe plus" in res["error"]["message"]
    res = engine.wait(engine.send("nimporte_quoi"))
    assert res["error"]["kind"] == "internal"
    res = engine.wait(engine.send("search", mauvais_param=1))       # mauvais appel : erreur, pas de plantage
    assert "error" in res
    assert engine.call("ping") == "pong"


def test_cursus_et_demande(engine):
    info = engine.call("cursus_info", matricule="18M042")
    ids = [r["id"] for r in info["releves"] if r["selected"] and not r["is_word"]]
    out = engine.call("cursus_build", matricule="18M042", releve_ids=ids, cover=True)
    assert Path(out["path"]).exists() and out["pages"] >= len(ids)
    d = engine.call("demande_create", matricule="18M042", nom="TCHOUA Alain", demandeur="Lui-même")
    did = d["demande"]["id"]
    for i in ids[:2]:
        d = engine.call("demande_add", did=did, releve_id=i)
    assert d["demande"]["n"] == 2
    files = engine.call("demande_files_to_print", did=did, with_bordereau=True)
    assert all(Path(f).exists() for f in files["files"]) and files["demande"]["statut"] == "PRÊTE"
    d = engine.call("demande_printed", did=did, how="test")
    assert d["demande"]["statut"] == "IMPRIMÉE"
    d = engine.call("demande_mark", did=did, statut="REMISE", detail="remis à lui-même")
    assert d["demande"]["statut"] == "REMISE"


def test_une_operation_longue_ne_bloque_pas_le_reste(engine, monkeypatch):
    """Plusieurs demandes en vol : le ping répond pendant qu'elles tournent."""
    ids = [engine.send("cursus_info", matricule="18M042") for _ in range(6)]
    t = time.time()
    assert engine.call("ping", timeout=5) == "pong" and time.time() - t < 2
    for i in ids:
        assert "result" in engine.wait(i)


def test_verification_par_le_moteur(engine):
    rel = next(r for r in engine.call("search", q="18M042")["results"] if not r["is_word"])
    out = engine.call("export", releve_id=rel["id"])
    ident = engine.call("verify_identify", path=out["path"])
    assert ident["matricule"] == "18M042" and ident["delivered"] and ident["best"] == rel["id"]
    cmp = engine.call("verify_compare", path=out["path"], releve_id=rel["id"])
    assert cmp["total"] > 0 and cmp["conformes"] == cmp["total"]


def test_fin_de_l_entree_arrete_le_moteur(indexed, tmp_path):
    from core.config import save_settings
    db, settings, _a, _ = indexed
    env = dict(os.environ, FMSP_DATA_DIR=str(tmp_path), PYTHONIOENCODING="utf-8")
    old = os.environ.get("FMSP_DATA_DIR")
    os.environ["FMSP_DATA_DIR"] = str(tmp_path)
    try:
        save_settings(settings)
    finally:
        os.environ["FMSP_DATA_DIR"] = old or ""
    c = Client(env)
    c.proc.stdin.close()                         # Electron fermé ou planté
    assert c.proc.wait(30) == 0
    assert (tmp_path / "logs" / "moteur.log").exists()


def test_vider_l_index_conserve_la_tracabilite(indexed, tmp_path):
    """« Vider l'index » retire relevés, fichiers et anomalies ; garde journal, registre et demandes."""
    import shutil

    from core.config import save_settings
    db, settings, archives, _ = indexed
    home = tmp_path / "data"
    home.mkdir()
    copy_db = tmp_path / "index.db"
    db.conn.execute("PRAGMA wal_checkpoint(FULL)")
    shutil.copy(settings.db_path, copy_db)
    import dataclasses
    s2 = dataclasses.replace(settings, db_path=str(copy_db), cache_dir=str(tmp_path / "cache"),
                             exports_root=str(tmp_path / "Exports"))
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "apercu.pdf").write_bytes(b"%PDF")
    old = os.environ.get("FMSP_DATA_DIR")
    os.environ["FMSP_DATA_DIR"] = str(home)
    try:
        save_settings(s2)
    finally:
        os.environ["FMSP_DATA_DIR"] = old or ""
    c = Client(dict(os.environ, FMSP_DATA_DIR=str(home), PYTHONIOENCODING="utf-8"))
    try:
        rel = next(r for r in c.call("search", q="18M042")["results"] if not r["is_word"])
        c.call("export", releve_id=rel["id"])                       # une délivrance au registre
        c.call("demande_create", matricule="18M042", nom="TCHOUA Alain", demandeur="test")
        before = c.call("index_impact")
        assert before["releves"] > 0 and before["delivrances"] >= 1
        res = c.wait(c.send("index_reset"))
        assert "error" in res                                        # sans confirmation : refusé
        out = c.call("index_reset", confirm="VIDER")
        assert out["releves"] == before["releves"] and out["stats"]["releves"] == 0
        after = c.call("index_impact")
        assert after["releves"] == after["fichiers"] == after["anomalies"] == 0
        assert after["delivrances"] == before["delivrances"] and after["demandes"] == before["demandes"]
        assert after["journal"] >= before["journal"]                  # le vidage lui-même est journalisé
        assert c.call("search", q="18M042")["results"] == []
        assert not (tmp_path / "cache" / "apercu.pdf").exists()
    finally:
        c.close()
